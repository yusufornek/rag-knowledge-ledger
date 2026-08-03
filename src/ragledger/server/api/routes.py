"""`/api/v1` route handlers for M7 wave B's first slice.

Implemented here, against the design specification section 16's surface:

- ``POST /auth/bootstrap`` (FR-001): first-run local admin bootstrap.
- ``GET /workspaces``, ``GET/PATCH /workspaces/{id}``.
- ``GET/POST /workspaces/{id}/api-tokens``, ``DELETE .../{token_id}``
  (FR-002; DELETE revokes rather than erases, preserving the audit
  trail a hard delete would orphan).
- ``GET/POST /workspaces/{id}/targets``, ``GET/PATCH/DELETE
  .../{target_id}`` (FR-003/FR-004).
- ``GET /workspaces/{id}/audit-events``.

Build/snapshot/reconciliation execution routes are the next wave B
slice -- they need the job orchestration layer (section 21) this slice
does not include.

Every mutating handler writes an `AuditEvent` in the same transaction
as the mutation itself, so an audit row never describes a change that
rolled back (see `ragledger.server.audit.AuditLog`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ragledger.server.api.deps import AuthContext, authenticate, require_scope, require_workspace
from ragledger.server.api.problems import ProblemException, problem_type
from ragledger.server.api.schemas import (
    ApiTokenCreated,
    ApiTokenCreateRequest,
    ApiTokenOut,
    AuditEventOut,
    BootstrapRequest,
    BootstrapResponse,
    TargetCreateRequest,
    TargetOut,
    TargetUpdateRequest,
    WorkspaceOut,
    WorkspaceUpdateRequest,
)
from ragledger.server.app import get_db_session
from ragledger.server.audit import AuditLog
from ragledger.server.db.models import (
    ApiToken,
    AuditEvent,
    Membership,
    User,
    VectorTarget,
    Workspace,
)
from ragledger.server.db.models.enums import MembershipRole
from ragledger.server.security import encrypt_credential, issue_api_token
from ragledger.server.settings import Settings
from ragledger.server.ssrf import TargetUrlNotAllowedError, validate_target_url

__all__ = ["api_router"]

api_router = APIRouter()

DbSession = Annotated[Session, Depends(get_db_session)]


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _not_found(what: str) -> ProblemException:
    return ProblemException(
        status=404,
        title="Not found",
        detail=f"{what} not found",
        problem_type=problem_type("not-found"),
    )


# --------------------------------------------------------------------------
# Bootstrap (FR-001)
# --------------------------------------------------------------------------


@api_router.post("/auth/bootstrap", response_model=BootstrapResponse, status_code=201)
def bootstrap(payload: BootstrapRequest, request: Request, db: DbSession) -> BootstrapResponse:
    """First-run local admin bootstrap: first user, first workspace, first admin token.

    Only works while the instance has no users at all; afterwards it is
    a 409, permanently -- additional workspaces/tokens come from the
    authenticated endpoints. This is what makes an unauthenticated
    bootstrap route safe to leave enabled: it is a no-op on any
    instance that has already been claimed.
    """
    existing_user = db.execute(select(User.id).limit(1)).first()
    if existing_user is not None:
        raise ProblemException(
            status=409,
            title="Already bootstrapped",
            detail="this instance already has a user; bootstrap can only run once",
            problem_type=problem_type("already-bootstrapped"),
        )

    user = User(email=payload.email, display_name=payload.display_name)
    workspace = Workspace(slug=payload.workspace_slug, name=payload.workspace_name)
    db.add_all([user, workspace])
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id, role=MembershipRole.OWNER))

    issued = issue_api_token(prefix=_settings(request).api_token_prefix)
    token_row = ApiToken(
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        name="bootstrap admin token",
        prefix=issued.prefix,
        selector=issued.selector,
        salt=issued.salt,
        token_hash=issued.token_hash,
        scopes=["admin"],
    )
    db.add(token_row)
    db.flush()

    AuditLog(db).record(
        actor_type="user",
        actor_id=str(user.id),
        action="auth.bootstrap",
        result="success",
        workspace_id=workspace.id,
        entity_type="workspace",
        entity_id=str(workspace.id),
        request_id=_request_id(request),
        metadata={"workspace_slug": workspace.slug},
    )
    db.commit()

    return BootstrapResponse(
        user_id=user.id,
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        token=issued.token,
        token_id=token_row.id,
        token_scopes=list(token_row.scopes),
    )


# --------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------


def _workspace_out(workspace: Workspace) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        slug=workspace.slug,
        name=workspace.name,
        created_at=workspace.created_at,
    )


@api_router.get("/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(
    db: DbSession, auth: Annotated[AuthContext, Depends(authenticate)]
) -> list[WorkspaceOut]:
    """The workspaces this token can see: exactly the one it belongs to.

    An API token is workspace-bound (FR-002), so this is a
    single-element list by construction; the endpoint exists so a
    caller can discover its own workspace id from a bare token.
    """
    workspace = db.get(Workspace, auth.workspace_id)
    if workspace is None:  # the workspace was deleted out from under a live token
        return []
    return [_workspace_out(workspace)]


@api_router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, Depends(require_workspace)],
) -> WorkspaceOut:
    workspace = db.get(Workspace, auth.workspace_id)
    if workspace is None:
        raise _not_found("workspace")
    return _workspace_out(workspace)


@api_router.patch("/workspaces/{workspace_id}", response_model=WorkspaceOut)
def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("admin")],
) -> WorkspaceOut:
    workspace = db.get(Workspace, auth.workspace_id)
    if workspace is None:
        raise _not_found("workspace")
    workspace.name = payload.name
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="workspace.update",
        result="success",
        workspace_id=workspace.id,
        entity_type="workspace",
        entity_id=str(workspace.id),
        request_id=_request_id(request),
    )
    db.commit()
    return _workspace_out(workspace)


# --------------------------------------------------------------------------
# API tokens (FR-002)
# --------------------------------------------------------------------------


def _token_out(row: ApiToken) -> ApiTokenOut:
    return ApiTokenOut(
        id=row.id,
        name=row.name,
        selector=row.selector,
        scopes=list(row.scopes),
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


@api_router.get("/workspaces/{workspace_id}/api-tokens", response_model=list[ApiTokenOut])
def list_api_tokens(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("admin")],
) -> list[ApiTokenOut]:
    rows = (
        db.execute(
            select(ApiToken)
            .where(ApiToken.workspace_id == auth.workspace_id)
            .order_by(ApiToken.created_at)
        )
        .scalars()
        .all()
    )
    return [_token_out(row) for row in rows]


@api_router.post(
    "/workspaces/{workspace_id}/api-tokens", response_model=ApiTokenCreated, status_code=201
)
def create_api_token(
    workspace_id: uuid.UUID,
    payload: ApiTokenCreateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("admin")],
) -> ApiTokenCreated:
    if payload.expires_at is not None and payload.expires_at <= datetime.now(UTC):
        raise ProblemException(
            status=422,
            title="Invalid expiry",
            detail="expires_at must be in the future",
            problem_type=problem_type("invalid-expiry"),
        )
    issued = issue_api_token(prefix=_settings(request).api_token_prefix)
    row = ApiToken(
        workspace_id=auth.workspace_id,
        name=payload.name,
        prefix=issued.prefix,
        selector=issued.selector,
        salt=issued.salt,
        token_hash=issued.token_hash,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
    )
    db.add(row)
    db.flush()
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="api_token.create",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="api_token",
        entity_id=str(row.id),
        request_id=_request_id(request),
        metadata={"scopes": payload.scopes},
    )
    db.commit()
    return ApiTokenCreated(**_token_out(row).model_dump(), token=issued.token)


@api_router.delete("/workspaces/{workspace_id}/api-tokens/{token_id}", status_code=204)
def revoke_api_token(
    workspace_id: uuid.UUID,
    token_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("admin")],
) -> None:
    row = db.get(ApiToken, token_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("api token")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        AuditLog(db).record(
            actor_type="api_token",
            actor_id=str(auth.token_id),
            action="api_token.revoke",
            result="success",
            workspace_id=auth.workspace_id,
            entity_type="api_token",
            entity_id=str(row.id),
            request_id=_request_id(request),
        )
        db.commit()


# --------------------------------------------------------------------------
# Targets (FR-003 / FR-004)
# --------------------------------------------------------------------------


def _target_out(row: VectorTarget) -> TargetOut:
    return TargetOut(
        id=row.id,
        name=row.name,
        target_type=row.target_type,
        endpoint_redacted=row.endpoint_redacted,
        mapping_config=row.mapping_config,
        credential_configured=True,
        credential_key_id=row.credential_key_id,
        credential_version=row.credential_version,
        allowlist_decision=row.allowlist_decision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_endpoint(url: str, settings: Settings) -> Any:
    try:
        return validate_target_url(url, settings=settings)
    except TargetUrlNotAllowedError as exc:
        raise ProblemException(
            status=422,
            title="Target URL not allowed",
            detail=str(exc),
            problem_type=problem_type("target-url-not-allowed"),
        ) from exc


def _encrypt(credential: str, settings: Settings) -> tuple[bytes, str]:
    try:
        return encrypt_credential(credential.encode("utf-8"), settings=settings)
    except RuntimeError as exc:
        # No APP_ENCRYPTION_KEY_V<n> configured: an operator problem,
        # not a caller problem.
        raise ProblemException(
            status=503,
            title="Credential encryption unavailable",
            detail="no credential encryption key is configured on this server",
            problem_type=problem_type("encryption-unavailable"),
        ) from exc


@api_router.get("/workspaces/{workspace_id}/targets", response_model=list[TargetOut])
def list_targets(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("targets")],
) -> list[TargetOut]:
    rows = (
        db.execute(
            select(VectorTarget)
            .where(VectorTarget.workspace_id == auth.workspace_id)
            .order_by(VectorTarget.created_at)
        )
        .scalars()
        .all()
    )
    return [_target_out(row) for row in rows]


@api_router.post("/workspaces/{workspace_id}/targets", response_model=TargetOut, status_code=201)
def create_target(
    workspace_id: uuid.UUID,
    payload: TargetCreateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("targets")],
) -> TargetOut:
    settings = _settings(request)
    validation = _validate_endpoint(payload.endpoint_url, settings)
    ciphertext, key_id = _encrypt(payload.credential, settings)
    row = VectorTarget(
        workspace_id=auth.workspace_id,
        name=payload.name,
        target_type=payload.target_type,
        endpoint_redacted=validation.endpoint_redacted,
        mapping_config=payload.mapping_config,
        credential_ciphertext=ciphertext,
        credential_key_id=key_id,
        credential_version=1,
        allowlist_decision=validation.decision,
    )
    db.add(row)
    db.flush()
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="target.create",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="vector_target",
        entity_id=str(row.id),
        request_id=_request_id(request),
        metadata={
            "target_type": payload.target_type.value,
            "endpoint_redacted": validation.endpoint_redacted,
            "allowlist_decision": validation.decision,
        },
    )
    db.commit()
    return _target_out(row)


def _get_workspace_target(db: Session, auth: AuthContext, target_id: uuid.UUID) -> VectorTarget:
    row = db.get(VectorTarget, target_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("target")
    return row


@api_router.get("/workspaces/{workspace_id}/targets/{target_id}", response_model=TargetOut)
def get_target(
    workspace_id: uuid.UUID,
    target_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("targets")],
) -> TargetOut:
    return _target_out(_get_workspace_target(db, auth, target_id))


@api_router.patch("/workspaces/{workspace_id}/targets/{target_id}", response_model=TargetOut)
def update_target(
    workspace_id: uuid.UUID,
    target_id: uuid.UUID,
    payload: TargetUpdateRequest,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("targets")],
) -> TargetOut:
    settings = _settings(request)
    row = _get_workspace_target(db, auth, target_id)
    changed: list[str] = []
    if payload.name is not None:
        row.name = payload.name
        changed.append("name")
    if payload.mapping_config is not None:
        row.mapping_config = payload.mapping_config
        changed.append("mapping_config")
    if payload.endpoint_url is not None:
        validation = _validate_endpoint(payload.endpoint_url, settings)
        row.endpoint_redacted = validation.endpoint_redacted
        row.allowlist_decision = validation.decision
        changed.append("endpoint")
    if payload.credential is not None:
        ciphertext, key_id = _encrypt(payload.credential, settings)
        row.credential_ciphertext = ciphertext
        row.credential_key_id = key_id
        row.credential_version = row.credential_version + 1
        changed.append("credential")
    if changed:
        AuditLog(db).record(
            actor_type="api_token",
            actor_id=str(auth.token_id),
            action="target.update",
            result="success",
            workspace_id=auth.workspace_id,
            entity_type="vector_target",
            entity_id=str(row.id),
            request_id=_request_id(request),
            metadata={"changed": changed},
        )
        db.commit()
    return _target_out(row)


@api_router.delete("/workspaces/{workspace_id}/targets/{target_id}", status_code=204)
def delete_target(
    workspace_id: uuid.UUID,
    target_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("targets")],
) -> None:
    row = _get_workspace_target(db, auth, target_id)
    db.delete(row)
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="target.delete",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="vector_target",
        entity_id=str(target_id),
        request_id=_request_id(request),
    )
    db.commit()


# --------------------------------------------------------------------------
# Audit events
# --------------------------------------------------------------------------


@api_router.get("/workspaces/{workspace_id}/audit-events", response_model=list[AuditEventOut])
def list_audit_events(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("admin")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before: Annotated[datetime | None, Query()] = None,
) -> list[AuditEventOut]:
    """Newest-first audit events, keyset-paginated by `created_at` via ``before``."""
    query = (
        select(AuditEvent)
        .where(AuditEvent.workspace_id == auth.workspace_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
    )
    if before is not None:
        query = query.where(AuditEvent.created_at < before)
    rows = db.execute(query).scalars().all()
    return [
        AuditEventOut(
            id=row.id,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            result=row.result,
            request_id=row.request_id,
            metadata=row.metadata_json,
            created_at=row.created_at,
        )
        for row in rows
    ]
