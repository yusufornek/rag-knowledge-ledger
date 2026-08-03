"""`/api/v1` routes for source collections, pipeline configs, builds, and jobs.

M7 wave B slice 2, against the design specification section 16's surface:

- ``GET|POST /workspaces/{id}/source-collections``
- ``POST /workspaces/{id}/source-collections/{cid}:scan`` (202 + job)
- ``GET /workspaces/{id}/sources``, ``GET .../sources/{sid}/versions``
- ``GET|POST /workspaces/{id}/pipeline-configs``
- ``GET|POST /workspaces/{id}/builds``, ``GET .../builds/{bid}``,
  ``POST .../builds/{bid}:cancel``
- ``GET /workspaces/{id}/jobs/{job_id}``

Asynchronous work goes through the DB job queue
(`ragledger.server.jobs`): the enqueueing request commits the job row
and schedules `run_pending_jobs` as a FastAPI background task, so in a
single-process deployment (and in tests) the job executes right after
the response is sent. A dedicated worker process leasing the same table
supersedes the background task transparently -- whichever leases first
wins, the other sees an empty queue. Cancellation covers queued builds
only in this slice; see `ragledger.server.jobs`' module docstring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ragledger.core.hashing import hash_canonical
from ragledger.server.api.deps import AuthContext, require_scope
from ragledger.server.api.pipeline_schemas import (
    BuildCreateRequest,
    BuildOut,
    JobOut,
    PipelineConfigCreateRequest,
    PipelineConfigOut,
    SourceAssetOut,
    SourceCollectionCreateRequest,
    SourceCollectionOut,
    SourceVersionOut,
)
from ragledger.server.api.problems import ProblemException, problem_type
from ragledger.server.api.routes import _not_found, _request_id, _settings
from ragledger.server.app import get_db_session
from ragledger.server.audit import AuditLog
from ragledger.server.db.models import (
    Build,
    Job,
    Manifest,
    PipelineConfig,
    SourceAsset,
    SourceCollection,
    SourceVersion,
)
from ragledger.server.db.models.enums import BuildState
from ragledger.server.handlers import (
    JOB_TYPE_BUILD,
    JOB_TYPE_SCAN,
    make_cancel_finalizers,
    make_handlers,
)
from ragledger.server.jobs import CancelOutcome, enqueue_job, request_cancel, run_pending_jobs

__all__ = ["pipeline_router"]

pipeline_router = APIRouter()

DbSession = Annotated[Session, Depends(get_db_session)]


def _schedule_job_execution(request: Request, background: BackgroundTasks) -> None:
    """Run the queue after this request commits (single-process execution path)."""
    settings = _settings(request)
    session_factory = request.app.state.session_factory
    background.add_task(
        run_pending_jobs,
        session_factory,
        make_handlers(settings),
        finalizers=make_cancel_finalizers(),
    )


def _latest_job_for(
    db: Session, entity_type: str, entity_id: uuid.UUID, *, for_update: bool = False
) -> Job | None:
    query = (
        select(Job)
        .where(Job.related_entity_type == entity_type, Job.related_entity_id == str(entity_id))
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    return db.execute(query).scalar_one_or_none()


def _cancel_entity_job(db: Session, entity_type: str, entity_id: uuid.UUID) -> str:
    """Request cancellation of an entity's latest job; raise a 409 problem if terminal."""
    job = _latest_job_for(db, entity_type, entity_id, for_update=True)
    outcome = CancelOutcome.NOT_CANCELLABLE if job is None else request_cancel(db, job)
    if outcome == CancelOutcome.NOT_CANCELLABLE:
        raise ProblemException(
            status=409,
            title="Not cancellable",
            detail=f"this {entity_type}'s job already finished; nothing to cancel",
            problem_type=problem_type("not-cancellable"),
        )
    return outcome


def _job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        attempt_count=job.attempt_count,
        last_error=job.last_error,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


# --------------------------------------------------------------------------
# Source collections
# --------------------------------------------------------------------------


def _collection_out(row: SourceCollection) -> SourceCollectionOut:
    return SourceCollectionOut(
        id=row.id,
        name=row.name,
        namespace=row.namespace,
        root=str(row.root_config.get("root", "")),
        created_at=row.created_at,
    )


@pipeline_router.get(
    "/workspaces/{workspace_id}/source-collections", response_model=list[SourceCollectionOut]
)
def list_source_collections(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("sources")],
) -> list[SourceCollectionOut]:
    rows = (
        db.execute(
            select(SourceCollection)
            .where(SourceCollection.workspace_id == auth.workspace_id)
            .order_by(SourceCollection.created_at)
        )
        .scalars()
        .all()
    )
    return [_collection_out(row) for row in rows]


@pipeline_router.post(
    "/workspaces/{workspace_id}/source-collections",
    response_model=SourceCollectionOut,
    status_code=201,
)
def create_source_collection(
    workspace_id: uuid.UUID,
    payload: SourceCollectionCreateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("sources")],
) -> SourceCollectionOut:
    settings = _settings(request)
    root = Path(payload.root)
    if not root.is_dir():
        raise ProblemException(
            status=422,
            title="Invalid source root",
            detail="root is not an existing directory on the server",
            problem_type=problem_type("invalid-source-root"),
        )
    if not settings.is_source_root_allowed(root):
        raise ProblemException(
            status=422,
            title="Source root not allowed",
            detail="root is outside SOURCE_ROOT_ALLOWED_BASES (or no bases are configured "
            "in production)",
            problem_type=problem_type("source-root-not-allowed"),
        )
    duplicate = db.execute(
        select(SourceCollection.id).where(
            SourceCollection.workspace_id == auth.workspace_id,
            SourceCollection.namespace == payload.namespace,
        )
    ).first()
    if duplicate is not None:
        raise ProblemException(
            status=409,
            title="Namespace already exists",
            detail=f"a source collection with namespace {payload.namespace!r} already exists",
            problem_type=problem_type("duplicate-namespace"),
        )
    row = SourceCollection(
        workspace_id=auth.workspace_id,
        name=payload.name,
        namespace=payload.namespace,
        root_config={"root": str(root.resolve())},
    )
    db.add(row)
    db.flush()
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="source_collection.create",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="source_collection",
        entity_id=str(row.id),
        request_id=_request_id(request),
        metadata={"namespace": payload.namespace},
    )
    db.commit()
    return _collection_out(row)


@pipeline_router.post(
    "/workspaces/{workspace_id}/source-collections/{collection_id}:scan",
    response_model=JobOut,
    status_code=202,
)
def scan_source_collection(
    workspace_id: uuid.UUID,
    collection_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("sources")],
) -> JobOut:
    collection = db.get(SourceCollection, collection_id)
    if collection is None or collection.workspace_id != auth.workspace_id:
        raise _not_found("source collection")
    job = enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        job_type=JOB_TYPE_SCAN,
        payload={"collection_id": str(collection.id)},
        related_entity_type="source_collection",
        related_entity_id=str(collection.id),
    )
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="source_collection.scan",
        result="queued",
        workspace_id=auth.workspace_id,
        entity_type="source_collection",
        entity_id=str(collection.id),
        request_id=_request_id(request),
        metadata={"job_id": str(job.id)},
    )
    db.commit()
    _schedule_job_execution(request, background)
    return _job_out(job)


@pipeline_router.get("/workspaces/{workspace_id}/sources", response_model=list[SourceAssetOut])
def list_sources(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("sources")],
    collection_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[SourceAssetOut]:
    query = (
        select(SourceAsset)
        .where(SourceAsset.workspace_id == auth.workspace_id)
        .order_by(SourceAsset.uri)
    )
    if collection_id is not None:
        query = query.where(SourceAsset.collection_id == collection_id)
    rows = db.execute(query).scalars().all()
    return [
        SourceAssetOut(
            id=row.id,
            collection_id=row.collection_id,
            portable_id=row.portable_id,
            uri=row.uri,
            status=row.status,
        )
        for row in rows
    ]


@pipeline_router.get(
    "/workspaces/{workspace_id}/sources/{source_id}/versions",
    response_model=list[SourceVersionOut],
)
def list_source_versions(
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("sources")],
) -> list[SourceVersionOut]:
    asset = db.get(SourceAsset, source_id)
    if asset is None or asset.workspace_id != auth.workspace_id:
        raise _not_found("source")
    rows = (
        db.execute(
            select(SourceVersion)
            .where(SourceVersion.source_asset_id == asset.id)
            .order_by(SourceVersion.created_at)
        )
        .scalars()
        .all()
    )
    return [
        SourceVersionOut(
            id=row.id,
            portable_id=row.portable_id,
            content_hash=row.content_hash,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            created_at=row.created_at,
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# Pipeline configs
# --------------------------------------------------------------------------


def _pipeline_config_out(row: PipelineConfig) -> PipelineConfigOut:
    return PipelineConfigOut(
        id=row.id, config_hash=row.config_hash, config=row.config_json, created_at=row.created_at
    )


@pipeline_router.get(
    "/workspaces/{workspace_id}/pipeline-configs", response_model=list[PipelineConfigOut]
)
def list_pipeline_configs(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> list[PipelineConfigOut]:
    rows = (
        db.execute(
            select(PipelineConfig)
            .where(PipelineConfig.workspace_id == auth.workspace_id)
            .order_by(PipelineConfig.created_at)
        )
        .scalars()
        .all()
    )
    return [_pipeline_config_out(row) for row in rows]


@pipeline_router.post(
    "/workspaces/{workspace_id}/pipeline-configs", response_model=PipelineConfigOut, status_code=201
)
def create_pipeline_config(
    workspace_id: uuid.UUID,
    payload: PipelineConfigCreateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> PipelineConfigOut:
    """Create (or return the existing) immutable pipeline config for this exact content.

    Configs are content-addressed by their canonical hash: posting the
    same config twice returns the same row, per section 15's immutable
    `PipelineConfig` design.
    """
    config_json: dict[str, Any] = payload.config.model_dump(mode="json")
    config_hash = hash_canonical(config_json)
    existing = db.execute(
        select(PipelineConfig).where(
            PipelineConfig.workspace_id == auth.workspace_id,
            PipelineConfig.config_hash == config_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _pipeline_config_out(existing)
    row = PipelineConfig(
        workspace_id=auth.workspace_id, config_hash=config_hash, config_json=config_json
    )
    db.add(row)
    db.flush()
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="pipeline_config.create",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="pipeline_config",
        entity_id=str(row.id),
        request_id=_request_id(request),
        metadata={"config_hash": config_hash},
    )
    db.commit()
    return _pipeline_config_out(row)


# --------------------------------------------------------------------------
# Builds
# --------------------------------------------------------------------------


def _build_out(db: Session, row: Build) -> BuildOut:
    # A byte-identical rebuild dedupes onto the manifest row the first
    # build created (unique on workspace/manifest_hash), so the lookup
    # goes through the build's recorded manifest hash, not `build_id`.
    manifest = None
    manifest_hash = row.counters.get("manifest_hash")
    if manifest_hash:
        manifest = db.execute(
            select(Manifest).where(
                Manifest.workspace_id == row.workspace_id,
                Manifest.manifest_hash == manifest_hash,
            )
        ).scalar_one_or_none()
    job = db.execute(
        select(Job)
        .where(Job.related_entity_type == "build", Job.related_entity_id == str(row.id))
        .order_by(Job.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return BuildOut(
        id=row.id,
        source_collection_id=row.source_collection_id,
        pipeline_config_id=row.pipeline_config_id,
        state=row.state,
        counters=row.counters,
        job_id=job.id if job is not None else None,
        manifest_id=manifest.id if manifest is not None else None,
        manifest_hash=manifest.manifest_hash if manifest is not None else None,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


@pipeline_router.get("/workspaces/{workspace_id}/builds", response_model=list[BuildOut])
def list_builds(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> list[BuildOut]:
    rows = (
        db.execute(
            select(Build)
            .where(Build.workspace_id == auth.workspace_id)
            .order_by(Build.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [_build_out(db, row) for row in rows]


@pipeline_router.post("/workspaces/{workspace_id}/builds", response_model=BuildOut, status_code=202)
def create_build(
    workspace_id: uuid.UUID,
    payload: BuildCreateRequest,
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> BuildOut:
    collection = db.get(SourceCollection, payload.source_collection_id)
    if collection is None or collection.workspace_id != auth.workspace_id:
        raise _not_found("source collection")
    pipeline_config = db.get(PipelineConfig, payload.pipeline_config_id)
    if pipeline_config is None or pipeline_config.workspace_id != auth.workspace_id:
        raise _not_found("pipeline config")

    build = Build(
        workspace_id=auth.workspace_id,
        source_collection_id=collection.id,
        pipeline_config_id=pipeline_config.id,
    )
    db.add(build)
    db.flush()
    job_payload: dict[str, Any] = {"build_id": str(build.id)}
    if payload.epoch is not None:
        job_payload["epoch"] = payload.epoch
    job = enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        job_type=JOB_TYPE_BUILD,
        payload=job_payload,
        related_entity_type="build",
        related_entity_id=str(build.id),
    )
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="build.create",
        result="queued",
        workspace_id=auth.workspace_id,
        entity_type="build",
        entity_id=str(build.id),
        request_id=_request_id(request),
        metadata={"job_id": str(job.id)},
    )
    db.commit()
    _schedule_job_execution(request, background)
    return _build_out(db, build)


def _get_workspace_build(db: Session, auth: AuthContext, build_id: uuid.UUID) -> Build:
    row = db.get(Build, build_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("build")
    return row


@pipeline_router.get("/workspaces/{workspace_id}/builds/{build_id}", response_model=BuildOut)
def get_build(
    workspace_id: uuid.UUID,
    build_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> BuildOut:
    return _build_out(db, _get_workspace_build(db, auth, build_id))


@pipeline_router.post(
    "/workspaces/{workspace_id}/builds/{build_id}:cancel", response_model=BuildOut
)
def cancel_build(
    workspace_id: uuid.UUID,
    build_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> BuildOut:
    """Cancel a build: outright while queued, cooperatively while running.

    A queued job flips straight to `CANCELLED` and the build with it; a
    running job gets its `cancel_requested` flag set and the worker
    honors it at its next check point, after which the cancel finalizer
    stamps the build `CANCELLED`. A build already finished is a 409.
    """
    build = _get_workspace_build(db, auth, build_id)
    outcome = _cancel_entity_job(db, "build", build.id)
    if outcome == CancelOutcome.CANCELLED and build.state == BuildState.PENDING:
        build.state = BuildState.CANCELLED
        build.completed_at = datetime.now(UTC)
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="build.cancel",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="build",
        entity_id=str(build.id),
        request_id=_request_id(request),
    )
    db.commit()
    return _build_out(db, build)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


@pipeline_router.get("/workspaces/{workspace_id}/jobs", response_model=list[JobOut])
def list_jobs(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[JobOut]:
    """Newest-first jobs across all types, for dashboards and debugging."""
    rows = (
        db.execute(
            select(Job)
            .where(Job.workspace_id == auth.workspace_id)
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [_job_out(row) for row in rows]


@pipeline_router.get("/workspaces/{workspace_id}/jobs/{job_id}", response_model=JobOut)
def get_job(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> JobOut:
    job = db.get(Job, job_id)
    if job is None or job.workspace_id != auth.workspace_id:
        raise _not_found("job")
    return _job_out(job)
