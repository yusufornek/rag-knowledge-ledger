"""`/api/v1` routes for policies, reconciliations, and findings (M7 wave B slice 4).

Against PROJECT_SPEC.md section 16's surface:

- ``GET|POST /workspaces/{id}/policies``
- ``POST /workspaces/{id}/policies/{pid}/revisions``
- ``GET|POST /workspaces/{id}/reconciliations``
- ``GET /workspaces/{id}/reconciliations/{rid}``
- ``GET /workspaces/{id}/reconciliations/{rid}/findings``

A policy is a named pointer whose every edit is a new immutable
`PolicyRevision` (section 15.3); the document itself is validated at
the API boundary by the same `load_policy_document` path the CLI and
the reconcile engine use, so a stored revision can never fail to load
later. A reconciliation runs through the job queue against an already
captured snapshot artifact and a persisted manifest; its bounded
findings land in the `findings` table and the full report becomes a
content-addressed artifact (see
`ragledger.server.handlers.run_reconcile_job`).
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ragledger.core.hashing import hash_canonical
from ragledger.reconcile.policy import (
    PolicyValidationError,
    evaluate_policy,
    load_policy_document,
)
from ragledger.reconcile.report import ReconciliationReport
from ragledger.server.api.deps import AuthContext, require_scope
from ragledger.server.api.pipeline_routes import (
    _cancel_entity_job,
    _job_out,
    _schedule_job_execution,
)
from ragledger.server.api.pipeline_schemas import (
    FindingOut,
    PolicyCreateRequest,
    PolicyOut,
    PolicyRevisionCreateRequest,
    PolicyRevisionOut,
    ReconciliationCreateRequest,
    ReconciliationCreateResponse,
    ReconciliationOut,
)
from ragledger.server.api.problems import ProblemException, problem_type
from ragledger.server.api.routes import _not_found, _request_id, _settings
from ragledger.server.app import get_db_session
from ragledger.server.audit import AuditLog
from ragledger.server.db.models import (
    Finding,
    InventorySnapshot,
    Job,
    Manifest,
    Policy,
    PolicyRevision,
    Reconciliation,
)
from ragledger.server.db.models.enums import FindingSeverity, ReconciliationState
from ragledger.server.handlers import JOB_TYPE_RECONCILE
from ragledger.server.jobs import CancelOutcome, enqueue_job

__all__ = ["reconcile_router"]

reconcile_router = APIRouter()

DbSession = Annotated[Session, Depends(get_db_session)]


# --------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------


def _validate_policy_document(document: dict[str, Any]) -> None:
    try:
        load_policy_document(json.dumps(document), document_format="json")
    except (PolicyValidationError, ValidationError, ValueError) as exc:
        raise ProblemException(
            status=422,
            title="Invalid policy document",
            detail=str(exc),
            problem_type=problem_type("invalid-policy"),
        ) from exc


def _latest_revision(db: Session, policy: Policy) -> PolicyRevision | None:
    return db.execute(
        select(PolicyRevision)
        .where(PolicyRevision.policy_id == policy.id)
        .order_by(PolicyRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()


def _policy_out(db: Session, policy: Policy) -> PolicyOut:
    revision = _latest_revision(db, policy)
    return PolicyOut(
        id=policy.id,
        name=policy.name,
        created_at=policy.created_at,
        latest_revision=(
            PolicyRevisionOut(
                id=revision.id,
                revision_number=revision.revision_number,
                config_hash=revision.config_hash,
                document=revision.rules_json,
                created_at=revision.created_at,
            )
            if revision is not None
            else None
        ),
    )


@reconcile_router.get("/workspaces/{workspace_id}/policies", response_model=list[PolicyOut])
def list_policies(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("policies")],
) -> list[PolicyOut]:
    rows = (
        db.execute(
            select(Policy)
            .where(Policy.workspace_id == auth.workspace_id)
            .order_by(Policy.created_at)
        )
        .scalars()
        .all()
    )
    return [_policy_out(db, row) for row in rows]


@reconcile_router.post(
    "/workspaces/{workspace_id}/policies", response_model=PolicyOut, status_code=201
)
def create_policy(
    workspace_id: uuid.UUID,
    payload: PolicyCreateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("policies")],
) -> PolicyOut:
    _validate_policy_document(payload.document)
    duplicate = db.execute(
        select(Policy.id).where(
            Policy.workspace_id == auth.workspace_id, Policy.name == payload.name
        )
    ).first()
    if duplicate is not None:
        raise ProblemException(
            status=409,
            title="Policy name already exists",
            detail=f"a policy named {payload.name!r} already exists; add a revision instead",
            problem_type=problem_type("duplicate-policy-name"),
        )
    policy = Policy(workspace_id=auth.workspace_id, name=payload.name)
    db.add(policy)
    db.flush()
    db.add(
        PolicyRevision(
            workspace_id=auth.workspace_id,
            policy_id=policy.id,
            revision_number=1,
            config_hash=hash_canonical(payload.document),
            rules_json=payload.document,
        )
    )
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="policy.create",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="policy",
        entity_id=str(policy.id),
        request_id=_request_id(request),
        metadata={"name": payload.name},
    )
    db.commit()
    return _policy_out(db, policy)


@reconcile_router.post(
    "/workspaces/{workspace_id}/policies/{policy_id}/revisions",
    response_model=PolicyOut,
    status_code=201,
)
def create_policy_revision(
    workspace_id: uuid.UUID,
    policy_id: uuid.UUID,
    payload: PolicyRevisionCreateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("policies")],
) -> PolicyOut:
    policy = db.get(Policy, policy_id)
    if policy is None or policy.workspace_id != auth.workspace_id:
        raise _not_found("policy")
    _validate_policy_document(payload.document)
    latest = _latest_revision(db, policy)
    next_number = 1 if latest is None else latest.revision_number + 1
    config_hash = hash_canonical(payload.document)
    if latest is not None and latest.config_hash == config_hash:
        return _policy_out(db, policy)  # identical content: no new revision
    db.add(
        PolicyRevision(
            workspace_id=auth.workspace_id,
            policy_id=policy.id,
            revision_number=next_number,
            config_hash=config_hash,
            rules_json=payload.document,
        )
    )
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="policy.revise",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="policy",
        entity_id=str(policy.id),
        request_id=_request_id(request),
        metadata={"revision_number": next_number},
    )
    db.commit()
    return _policy_out(db, policy)


# --------------------------------------------------------------------------
# Reconciliations
# --------------------------------------------------------------------------


def _finding_out(row: Finding) -> FindingOut:
    return FindingOut(
        id=row.id,
        fingerprint=row.fingerprint,
        code=row.code,
        severity=row.severity,
        source_hash=row.source_hash,
        chunk_hash=row.chunk_hash,
        point_hash=row.point_hash,
        expected_evidence=row.expected_evidence,
        observed_evidence=row.observed_evidence,
        created_at=row.created_at,
    )


def _reconciliation_out(db: Session, row: Reconciliation) -> ReconciliationOut:
    finding_count = db.execute(
        select(func.count()).select_from(Finding).where(Finding.reconciliation_id == row.id)
    ).scalar_one()
    job = db.execute(
        select(Job)
        .where(Job.related_entity_type == "reconciliation", Job.related_entity_id == str(row.id))
        .order_by(Job.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return ReconciliationOut(
        id=row.id,
        manifest_id=row.manifest_id,
        snapshot_id=row.snapshot_id,
        policy_revision_id=row.policy_revision_id,
        state=row.state,
        summary=row.summary,
        finding_count=finding_count,
        job_id=job.id if job is not None else None,
        created_at=row.created_at,
    )


@reconcile_router.get(
    "/workspaces/{workspace_id}/reconciliations", response_model=list[ReconciliationOut]
)
def list_reconciliations(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("reconciliations")],
) -> list[ReconciliationOut]:
    rows = (
        db.execute(
            select(Reconciliation)
            .where(Reconciliation.workspace_id == auth.workspace_id)
            .order_by(Reconciliation.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [_reconciliation_out(db, row) for row in rows]


@reconcile_router.post(
    "/workspaces/{workspace_id}/reconciliations",
    response_model=ReconciliationCreateResponse,
    status_code=202,
)
def create_reconciliation(
    workspace_id: uuid.UUID,
    payload: ReconciliationCreateRequest,
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("reconciliations")],
) -> ReconciliationCreateResponse:
    manifest = db.get(Manifest, payload.manifest_id)
    if manifest is None or manifest.workspace_id != auth.workspace_id:
        raise _not_found("manifest")
    snapshot = db.get(InventorySnapshot, payload.snapshot_id)
    if snapshot is None or snapshot.workspace_id != auth.workspace_id:
        raise _not_found("snapshot")
    if snapshot.artifact_ref is None:
        raise ProblemException(
            status=409,
            title="Snapshot not ready",
            detail="the snapshot has no artifact yet; wait for its job to complete",
            problem_type=problem_type("snapshot-not-ready"),
        )

    policy_revision_id: uuid.UUID | None = None
    if payload.policy_id is not None:
        policy = db.get(Policy, payload.policy_id)
        if policy is None or policy.workspace_id != auth.workspace_id:
            raise _not_found("policy")
        revision = _latest_revision(db, policy)
        if revision is None:
            raise _not_found("policy revision")
        policy_revision_id = revision.id

    reconciliation = Reconciliation(
        workspace_id=auth.workspace_id,
        manifest_id=manifest.id,
        snapshot_id=snapshot.id,
        policy_revision_id=policy_revision_id,
    )
    db.add(reconciliation)
    db.flush()
    job = enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        job_type=JOB_TYPE_RECONCILE,
        payload={"reconciliation_id": str(reconciliation.id)},
        related_entity_type="reconciliation",
        related_entity_id=str(reconciliation.id),
    )
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="reconciliation.create",
        result="queued",
        workspace_id=auth.workspace_id,
        entity_type="reconciliation",
        entity_id=str(reconciliation.id),
        request_id=_request_id(request),
        metadata={"job_id": str(job.id)},
    )
    db.commit()
    _schedule_job_execution(request, background)
    return ReconciliationCreateResponse(
        reconciliation=_reconciliation_out(db, reconciliation), job=_job_out(job)
    )


@reconcile_router.get(
    "/workspaces/{workspace_id}/reconciliations/{reconciliation_id}",
    response_model=ReconciliationOut,
)
def get_reconciliation(
    workspace_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("reconciliations")],
) -> ReconciliationOut:
    row = db.get(Reconciliation, reconciliation_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("reconciliation")
    return _reconciliation_out(db, row)


@reconcile_router.post(
    "/workspaces/{workspace_id}/reconciliations/{reconciliation_id}:cancel",
    response_model=ReconciliationOut,
)
def cancel_reconciliation(
    workspace_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("reconciliations")],
) -> ReconciliationOut:
    row = db.get(Reconciliation, reconciliation_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("reconciliation")
    outcome = _cancel_entity_job(db, "reconciliation", row.id)
    if outcome == CancelOutcome.CANCELLED and row.state == ReconciliationState.PENDING:
        row.state = ReconciliationState.CANCELLED
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="reconciliation.cancel",
        result=outcome,
        workspace_id=auth.workspace_id,
        entity_type="reconciliation",
        entity_id=str(row.id),
        request_id=_request_id(request),
    )
    db.commit()
    return _reconciliation_out(db, row)


def _load_report(db: Session, row: Reconciliation, request: Request) -> ReconciliationReport:
    """Load the full report artifact a completed reconciliation produced."""
    if row.state != ReconciliationState.COMPLETED or not row.summary:
        raise ProblemException(
            status=409,
            title="Reconciliation not completed",
            detail="this reconciliation has no report yet",
            problem_type=problem_type("reconciliation-not-completed"),
        )
    report_ref = str(row.summary.get("report_artifact", ""))
    digest = report_ref.rpartition("/")[2]
    path = Path(_settings(request).artifact_store_root) / "artifacts" / digest
    if not digest or not path.is_file():
        raise ProblemException(
            status=500,
            title="Report artifact missing",
            detail="the reconciliation's report artifact is not present in the store",
            problem_type=problem_type("artifact-missing"),
        )
    return ReconciliationReport.model_validate_json(path.read_bytes())


@reconcile_router.post(
    "/workspaces/{workspace_id}/reconciliations/{reconciliation_id}:evaluate-policy"
)
def evaluate_policy_endpoint(
    workspace_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    payload: PolicyRevisionCreateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("policies")],
) -> dict[str, Any]:
    """Re-evaluate a completed reconciliation's stored result against a policy document.

    The document is evaluated as-posted (it does not need to be a saved
    policy), against the exact `ReconciliationResult` the run produced
    -- nothing is re-reconciled. No `PolicyEvaluation` row is written
    for an ad-hoc evaluation; attach a policy at creation time for a
    persisted gate record.
    """
    row = db.get(Reconciliation, reconciliation_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("reconciliation")
    _validate_policy_document(payload.document)
    document = load_policy_document(json.dumps(payload.document), document_format="json")
    report = _load_report(db, row, request)
    verdict = evaluate_policy(document, report.result)
    return verdict.model_dump(mode="json")


@reconcile_router.post(
    "/workspaces/{workspace_id}/reconciliations/{reconciliation_id}/remediation-plans"
)
def get_remediation_plan(
    workspace_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("reconciliations")],
    format: Annotated[str, Query(pattern="^(json|csv)$")] = "json",
) -> Any:
    """The read-only remediation plan (FR-133/FR-135) from the stored report.

    ``format=csv`` streams the plan's rows as CSV; destructive
    candidates are explicitly flagged in both shapes. Nothing here ever
    executes an action (FR-134).
    """
    row = db.get(Reconciliation, reconciliation_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("reconciliation")
    report = _load_report(db, row, request)
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for csv_row in report.remediation.to_csv_rows():
            writer.writerow(csv_row)
        return PlainTextResponse(buffer.getvalue(), media_type="text/csv")
    return report.remediation.model_dump(mode="json")


@reconcile_router.get(
    "/workspaces/{workspace_id}/reconciliations/{reconciliation_id}/lineage/{portable_id}",
    response_model=list[FindingOut],
)
def get_lineage_findings(
    workspace_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    portable_id: str,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("reconciliations")],
) -> list[FindingOut]:
    """Findings touching one portable id (source or chunk), for lineage drill-down."""
    reconciliation = db.get(Reconciliation, reconciliation_id)
    if reconciliation is None or reconciliation.workspace_id != auth.workspace_id:
        raise _not_found("reconciliation")
    rows = (
        db.execute(
            select(Finding)
            .where(
                Finding.reconciliation_id == reconciliation.id,
                (Finding.source_hash == portable_id) | (Finding.chunk_hash == portable_id),
            )
            .order_by(Finding.fingerprint)
        )
        .scalars()
        .all()
    )
    return [_finding_out(row) for row in rows]


@reconcile_router.get(
    "/workspaces/{workspace_id}/reconciliations/{reconciliation_id}/findings",
    response_model=list[FindingOut],
)
def list_findings(
    workspace_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("reconciliations")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    severity: Annotated[FindingSeverity | None, Query()] = None,
    code: Annotated[str | None, Query(max_length=64)] = None,
) -> list[FindingOut]:
    """Findings ordered by fingerprint (the engine's own stable order), filterable."""
    reconciliation = db.get(Reconciliation, reconciliation_id)
    if reconciliation is None or reconciliation.workspace_id != auth.workspace_id:
        raise _not_found("reconciliation")
    query = (
        select(Finding)
        .where(Finding.reconciliation_id == reconciliation.id)
        .order_by(Finding.fingerprint)
        .offset(offset)
        .limit(limit)
    )
    if severity is not None:
        query = query.where(Finding.severity == severity)
    if code is not None:
        query = query.where(Finding.code == code)
    rows = db.execute(query).scalars().all()
    return [_finding_out(row) for row in rows]
