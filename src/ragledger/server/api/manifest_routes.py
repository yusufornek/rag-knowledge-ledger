"""`/api/v1` routes for manifests and inventory snapshots (M7 wave B slice 3).

Against the design specification section 16's surface:

- ``GET /workspaces/{id}/manifests``, ``GET .../manifests/{mid}``
- ``POST /workspaces/{id}/manifests/{mid}:sign``
- ``POST /workspaces/{id}/manifests/{mid}:verify``
- ``GET|POST /workspaces/{id}/targets/{tid}/snapshots``
- ``GET /workspaces/{id}/snapshots/{sid}``

Signing uses the server's secret-mounted Ed25519 key
(`MANIFEST_SIGNING_KEY_FILE`, section 41); when unset, signing is a 503
feature-disabled problem, never a fabricated signature. Signing
appends to the manifest's embedded ``signatures[]`` array and stores
the result as a *new* content-addressed artifact -- the RFC 8785
signing view excludes signatures, so `manifest_hash` (the row's
portable identity) is unchanged, and the row simply points at the new
artifact. Verification reads the trust store directory
(`MANIFEST_TRUST_STORE_PATH`, every ``*.pub`` file) and returns the
core `verify_manifest` result verbatim; an unknown key is
`VALID_UNTRUSTED`, exactly as the CLI reports it (section 19.5).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ragledger.core.artifacts import ArtifactStore
from ragledger.core.manifest import canonical_manifest_bytes, load_manifest
from ragledger.core.models import ManifestEnvelope
from ragledger.core.signing import (
    fingerprint,
    read_private_key,
    read_public_key,
    sign_manifest,
    verify_manifest,
)
from ragledger.server.api.deps import AuthContext, require_scope
from ragledger.server.api.pipeline_routes import (
    _cancel_entity_job,
    _job_out,
    _schedule_job_execution,
)
from ragledger.server.api.problems import ProblemException, problem_type
from ragledger.server.api.routes import _not_found, _request_id, _settings
from ragledger.server.api.schemas import (
    ManifestOut,
    ManifestVerifyResponse,
    SignatureOut,
    SnapshotCreateResponse,
    SnapshotOut,
)
from ragledger.server.app import get_db_session
from ragledger.server.audit import AuditLog
from ragledger.server.db.models import (
    InventorySnapshot,
    Manifest,
    ManifestSignature,
    VectorTarget,
)
from ragledger.server.db.models.enums import SnapshotStatus
from ragledger.server.handlers import JOB_TYPE_SNAPSHOT
from ragledger.server.jobs import CancelOutcome, enqueue_job
from ragledger.server.settings import Settings

__all__ = ["manifest_router"]

manifest_router = APIRouter()

DbSession = Annotated[Session, Depends(get_db_session)]


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


def _manifest_out(row: Manifest) -> ManifestOut:
    return ManifestOut(
        id=row.id,
        build_id=row.build_id,
        namespace=row.namespace,
        manifest_hash=row.manifest_hash,
        status=row.status,
        source_count=row.source_count,
        chunk_count=row.chunk_count,
        embedding_count=row.embedding_count,
        signed=row.signed,
        signatures=[
            SignatureOut(key_id=sig.key_id, signed_at=sig.signed_at, issuer=sig.issuer)
            for sig in row.signatures
        ],
        created_at=row.created_at,
    )


def _get_workspace_manifest(db: Session, auth: AuthContext, manifest_id: uuid.UUID) -> Manifest:
    row = db.get(Manifest, manifest_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("manifest")
    return row


def _load_manifest_envelope(row: Manifest, settings: Settings) -> ManifestEnvelope:
    digest = row.artifact_ref.rpartition("/")[2]
    path = Path(settings.artifact_store_root) / "artifacts" / digest
    if not path.is_file():
        raise ProblemException(
            status=500,
            title="Manifest artifact missing",
            detail="the manifest's artifact is not present in the artifact store",
            problem_type=problem_type("artifact-missing"),
        )
    return load_manifest(path)


@manifest_router.get("/workspaces/{workspace_id}/manifests", response_model=list[ManifestOut])
def list_manifests(
    workspace_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> list[ManifestOut]:
    rows = (
        db.execute(
            select(Manifest)
            .where(Manifest.workspace_id == auth.workspace_id)
            .order_by(Manifest.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [_manifest_out(row) for row in rows]


@manifest_router.get(
    "/workspaces/{workspace_id}/manifests/{manifest_id}", response_model=ManifestOut
)
def get_manifest(
    workspace_id: uuid.UUID,
    manifest_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> ManifestOut:
    return _manifest_out(_get_workspace_manifest(db, auth, manifest_id))


@manifest_router.post(
    "/workspaces/{workspace_id}/manifests/{manifest_id}:sign", response_model=ManifestOut
)
def sign_manifest_endpoint(
    workspace_id: uuid.UUID,
    manifest_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("admin")],
) -> ManifestOut:
    settings = _settings(request)
    if not settings.signing_enabled:
        raise ProblemException(
            status=503,
            title="Signing not enabled",
            detail="MANIFEST_SIGNING_KEY_FILE is not configured on this server",
            problem_type=problem_type("signing-disabled"),
        )
    row = _get_workspace_manifest(db, auth, manifest_id)
    envelope = _load_manifest_envelope(row, settings)

    assert settings.manifest_signing_key_file is not None  # signing_enabled checked above
    private_key = read_private_key(settings.manifest_signing_key_file)
    signed_at = datetime.now(UTC)
    issuer = settings.manifest_signing_key_id
    signed = sign_manifest(envelope, private_key, signed_at=signed_at, issuer=issuer)
    new_signature = signed.signatures[-1]

    stored = ArtifactStore(settings.artifact_store_root).put(canonical_manifest_bytes(signed))
    row.artifact_ref = f"artifacts/{stored.sha256}"
    row.signed = True
    db.add(
        ManifestSignature(
            manifest_id=row.id,
            key_id=new_signature.key_id,
            signature=new_signature.signature,
            signed_at=signed_at,
            issuer=issuer,
        )
    )
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="manifest.sign",
        result="success",
        workspace_id=auth.workspace_id,
        entity_type="manifest",
        entity_id=str(row.id),
        request_id=_request_id(request),
        metadata={"key_id": new_signature.key_id},
    )
    db.commit()
    db.refresh(row)
    return _manifest_out(row)


def _load_trust_store(settings: Settings) -> dict[str, Ed25519PublicKey]:
    trusted: dict[str, Ed25519PublicKey] = {}
    store_path = settings.manifest_trust_store_path
    if store_path is not None and store_path.is_dir():
        for key_file in sorted(store_path.glob("*.pub")):
            key = read_public_key(key_file)
            trusted[fingerprint(key)] = key
    return trusted


@manifest_router.post(
    "/workspaces/{workspace_id}/manifests/{manifest_id}:verify",
    response_model=ManifestVerifyResponse,
)
def verify_manifest_endpoint(
    workspace_id: uuid.UUID,
    manifest_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("builds")],
) -> ManifestVerifyResponse:
    settings = _settings(request)
    row = _get_workspace_manifest(db, auth, manifest_id)
    envelope = _load_manifest_envelope(row, settings)
    result = verify_manifest(envelope, _load_trust_store(settings))
    return ManifestVerifyResponse(
        hash_valid=result.hash_valid,
        overall=result.overall.value,
        signatures=[
            {"key_id": item.key_id, "status": item.status.value} for item in result.signatures
        ],
    )


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------


def _snapshot_out(row: InventorySnapshot) -> SnapshotOut:
    return SnapshotOut(
        id=row.id,
        target_id=row.target_id,
        status=row.status,
        point_count=row.point_count,
        content_hash=row.content_hash,
        schema_hash=row.schema_hash,
        created_at=row.created_at,
    )


def _get_workspace_target(db: Session, auth: AuthContext, target_id: uuid.UUID) -> VectorTarget:
    row = db.get(VectorTarget, target_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("target")
    return row


@manifest_router.get(
    "/workspaces/{workspace_id}/targets/{target_id}/snapshots",
    response_model=list[SnapshotOut],
)
def list_snapshots(
    workspace_id: uuid.UUID,
    target_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("snapshots")],
) -> list[SnapshotOut]:
    target = _get_workspace_target(db, auth, target_id)
    rows = (
        db.execute(
            select(InventorySnapshot)
            .where(InventorySnapshot.target_id == target.id)
            .order_by(InventorySnapshot.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [_snapshot_out(row) for row in rows]


@manifest_router.post(
    "/workspaces/{workspace_id}/targets/{target_id}/snapshots",
    response_model=SnapshotCreateResponse,
    status_code=202,
)
def create_snapshot(
    workspace_id: uuid.UUID,
    target_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("snapshots")],
) -> SnapshotCreateResponse:
    target = _get_workspace_target(db, auth, target_id)
    snapshot = InventorySnapshot(workspace_id=auth.workspace_id, target_id=target.id)
    db.add(snapshot)
    db.flush()
    job = enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        job_type=JOB_TYPE_SNAPSHOT,
        payload={"snapshot_id": str(snapshot.id)},
        related_entity_type="inventory_snapshot",
        related_entity_id=str(snapshot.id),
    )
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="snapshot.create",
        result="queued",
        workspace_id=auth.workspace_id,
        entity_type="inventory_snapshot",
        entity_id=str(snapshot.id),
        request_id=_request_id(request),
        metadata={"job_id": str(job.id), "target_id": str(target.id)},
    )
    db.commit()
    _schedule_job_execution(request, background)
    return SnapshotCreateResponse(snapshot=_snapshot_out(snapshot), job=_job_out(job))


@manifest_router.get(
    "/workspaces/{workspace_id}/snapshots/{snapshot_id}", response_model=SnapshotOut
)
def get_snapshot(
    workspace_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("snapshots")],
) -> SnapshotOut:
    row = db.get(InventorySnapshot, snapshot_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("snapshot")
    return _snapshot_out(row)


@manifest_router.post(
    "/workspaces/{workspace_id}/snapshots/{snapshot_id}:cancel", response_model=SnapshotOut
)
def cancel_snapshot(
    workspace_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    request: Request,
    db: DbSession,
    auth: Annotated[AuthContext, require_scope("snapshots")],
) -> SnapshotOut:
    """Cancel a snapshot: outright while queued, cooperatively while streaming.

    Per section 21: an already-written checkpoint artifact is retained
    and the snapshot's status becomes `cancelled`; a finished snapshot
    is a 409.
    """
    row = db.get(InventorySnapshot, snapshot_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise _not_found("snapshot")
    outcome = _cancel_entity_job(db, "inventory_snapshot", row.id)
    if outcome == CancelOutcome.CANCELLED and row.status == SnapshotStatus.PENDING:
        row.status = SnapshotStatus.CANCELLED
    AuditLog(db).record(
        actor_type="api_token",
        actor_id=str(auth.token_id),
        action="snapshot.cancel",
        result=outcome,
        workspace_id=auth.workspace_id,
        entity_type="inventory_snapshot",
        entity_id=str(row.id),
        request_id=_request_id(request),
    )
    db.commit()
    return _snapshot_out(row)
