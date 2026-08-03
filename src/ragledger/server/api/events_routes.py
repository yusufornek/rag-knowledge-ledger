"""`/api/v1` SSE progress events and the workspace export (M7 wave B final slice).

SSE (FR-144, section 16's ``.../events`` endpoints): every long-running
entity (build, snapshot, reconciliation) is driven by exactly one job
row, and the job row is the DB source of truth (section 21), so its
status *is* the progress stream. Each events endpoint resolves its
entity to that job and streams ``text/event-stream`` messages by
polling the row: one ``status`` event per observed change, and a final
``done`` event when the job reaches a terminal state. Polling the
source of truth keeps the stream correct under multiple workers and
process restarts -- a push channel can replace the poll later without
changing the wire format.

Workspace export (FR-005): a single JSON document of the workspace's
configuration and result metadata. Secrets and raw documents are
excluded by construction, not by filtering: the export is built from
the same response DTOs the API serves, none of which ever carry a
credential, token hash, or document body.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ragledger.server.api.deps import AuthContext, require_scope
from ragledger.server.api.manifest_routes import _manifest_out, _snapshot_out
from ragledger.server.api.pipeline_routes import (
    _collection_out,
    _latest_job_for,
    _pipeline_config_out,
)
from ragledger.server.api.reconcile_routes import _policy_out, _reconciliation_out
from ragledger.server.api.routes import _not_found, _target_out
from ragledger.server.app import get_db_session
from ragledger.server.db.models import (
    Build,
    InventorySnapshot,
    Job,
    Manifest,
    PipelineConfig,
    Policy,
    Reconciliation,
    SourceCollection,
    VectorTarget,
    Workspace,
)
from ragledger.server.db.models.enums import JobStatus

__all__ = ["events_router"]

events_router = APIRouter()

DbSession = Annotated[Session, Depends(get_db_session)]

_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
_POLL_INTERVAL_SECONDS = 0.5
_MAX_STREAM_SECONDS = 600.0


def _sse_message(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _job_event_stream(request: Request, job_id: uuid.UUID) -> AsyncIterator[str]:
    """Poll the job row and yield an SSE message per observed status change."""
    session_factory = request.app.state.session_factory
    last_status: JobStatus | None = None
    elapsed = 0.0
    while elapsed <= _MAX_STREAM_SECONDS:
        with session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                yield _sse_message("error", {"detail": "job no longer exists"})
                return
            status = job.status
            payload = {
                "job_id": str(job.id),
                "job_type": job.job_type,
                "status": status.value,
                "attempt_count": job.attempt_count,
                "last_error": job.last_error,
            }
        if status != last_status:
            last_status = status
            yield _sse_message("status", payload)
        if status in _TERMINAL_STATUSES:
            yield _sse_message("done", payload)
            return
        if await request.is_disconnected():
            return
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
    yield _sse_message("error", {"detail": "stream timeout; poll the job endpoint instead"})


def _sse_response(request: Request, job: Job) -> StreamingResponse:
    return StreamingResponse(
        _job_event_stream(request, job.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@events_router.get("/workspaces/{workspace_id}/builds/{build_id}/events")
def build_events(
    workspace_id: uuid.UUID,
    build_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> StreamingResponse:
    build = db.get(Build, build_id)
    if build is None or build.workspace_id != auth.workspace_id:
        raise _not_found("build")
    job = _latest_job_for(db, "build", build.id)
    if job is None:
        raise _not_found("build job")
    db.commit()  # release the row lock _latest_job_for takes before streaming
    return _sse_response(request, job)


@events_router.get("/workspaces/{workspace_id}/snapshots/{snapshot_id}/events")
def snapshot_events(
    workspace_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("snapshots")],
) -> StreamingResponse:
    snapshot = db.get(InventorySnapshot, snapshot_id)
    if snapshot is None or snapshot.workspace_id != auth.workspace_id:
        raise _not_found("snapshot")
    job = _latest_job_for(db, "inventory_snapshot", snapshot.id)
    if job is None:
        raise _not_found("snapshot job")
    db.commit()
    return _sse_response(request, job)


@events_router.get("/workspaces/{workspace_id}/reconciliations/{reconciliation_id}/events")
def reconciliation_events(
    workspace_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("reconciliations")],
) -> StreamingResponse:
    reconciliation = db.get(Reconciliation, reconciliation_id)
    if reconciliation is None or reconciliation.workspace_id != auth.workspace_id:
        raise _not_found("reconciliation")
    job = _latest_job_for(db, "reconciliation", reconciliation.id)
    if job is None:
        raise _not_found("reconciliation job")
    db.commit()
    return _sse_response(request, job)


# --------------------------------------------------------------------------
# Workspace export (FR-005)
# --------------------------------------------------------------------------


@events_router.get("/workspaces/{workspace_id}/export")
def export_workspace(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("admin")],
) -> dict[str, Any]:
    """Export the workspace's configuration and result metadata as one JSON document.

    FR-005: secrets and raw documents are excluded by default -- and
    here, by construction: every section is rendered through the same
    response DTOs the API itself serves, which never carry credentials,
    token secrets/hashes, or document bodies. There is deliberately no
    opt-in flag to include them.
    """
    workspace = db.get(Workspace, auth.workspace_id)
    if workspace is None:
        raise _not_found("workspace")

    def _rows(model: Any) -> list[Any]:
        return list(
            db.execute(
                select(model)
                .where(model.workspace_id == auth.workspace_id)
                .order_by(model.created_at)
            )
            .scalars()
            .all()
        )

    return {
        "export_version": 1,
        "workspace": {
            "id": str(workspace.id),
            "slug": workspace.slug,
            "name": workspace.name,
        },
        "source_collections": [
            _collection_out(row).model_dump(mode="json") for row in _rows(SourceCollection)
        ],
        "pipeline_configs": [
            _pipeline_config_out(row).model_dump(mode="json") for row in _rows(PipelineConfig)
        ],
        "targets": [_target_out(row).model_dump(mode="json") for row in _rows(VectorTarget)],
        "manifests": [_manifest_out(row).model_dump(mode="json") for row in _rows(Manifest)],
        "snapshots": [
            _snapshot_out(row).model_dump(mode="json") for row in _rows(InventorySnapshot)
        ],
        "policies": [_policy_out(db, row).model_dump(mode="json") for row in _rows(Policy)],
        "reconciliations": [
            _reconciliation_out(db, row).model_dump(mode="json") for row in _rows(Reconciliation)
        ],
    }
