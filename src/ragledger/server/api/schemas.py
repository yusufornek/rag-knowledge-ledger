"""Request/response DTOs for `/api/v1`, per PROJECT_SPEC.md section 16.

Section 16's closing rule governs every response model here: "Secret
plaintext hiçbir response DTO'da bulunmaz." A target response carries
`credential_configured=True` and the non-secret key id/version, never
the credential; a token response carries the bearable secret exactly
once, in the dedicated creation response (`ApiTokenCreated.token`), and
never again from any read endpoint.

Request models use ``extra="forbid"`` so an unknown field is a 422,
not a silently dropped key -- the same posture the policy loader takes
(FR-130) applied to the API boundary.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragledger.server.api.deps import API_TOKEN_SCOPES
from ragledger.server.api.pipeline_schemas import JobOut
from ragledger.server.db.models.enums import ManifestStatus, SnapshotStatus, VectorTargetType

__all__ = [
    "ApiTokenCreateRequest",
    "ApiTokenCreated",
    "ApiTokenOut",
    "AuditEventOut",
    "BootstrapRequest",
    "BootstrapResponse",
    "ManifestOut",
    "ManifestVerifyResponse",
    "SignatureOut",
    "SnapshotCreateResponse",
    "SnapshotOut",
    "TargetCreateRequest",
    "TargetOut",
    "TargetUpdateRequest",
    "WorkspaceOut",
    "WorkspaceUpdateRequest",
]

# Deliberately simple: enough to reject obvious garbage without pulling
# in the `email-validator` dependency for what is a local-admin field.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Bootstrap (FR-001)
# --------------------------------------------------------------------------


class BootstrapRequest(_RequestModel):
    email: str = Field(max_length=320)
    display_name: str | None = Field(default=None, max_length=255)
    workspace_slug: str = Field(max_length=63)
    workspace_name: str = Field(min_length=1, max_length=255)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value.lower()

    @field_validator("workspace_slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                "workspace_slug must be 3-63 lowercase letters, digits, or hyphens, "
                "starting and ending with a letter or digit"
            )
        return value


class BootstrapResponse(BaseModel):
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_slug: str
    token: str
    token_id: uuid.UUID
    token_scopes: list[str]


# --------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    created_at: datetime


class WorkspaceUpdateRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=255)


# --------------------------------------------------------------------------
# API tokens (FR-002)
# --------------------------------------------------------------------------


class ApiTokenCreateRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - API_TOKEN_SCOPES)
        if unknown:
            raise ValueError(
                f"unknown scopes {unknown}; valid scopes are {sorted(API_TOKEN_SCOPES)}"
            )
        return sorted(set(value))


class ApiTokenOut(BaseModel):
    id: uuid.UUID
    name: str
    selector: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class ApiTokenCreated(ApiTokenOut):
    """The creation response: the only place the bearable secret ever appears."""

    token: str


# --------------------------------------------------------------------------
# Targets (FR-003/FR-004)
# --------------------------------------------------------------------------


class TargetCreateRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=255)
    target_type: VectorTargetType
    endpoint_url: str = Field(min_length=1, max_length=2048)
    credential: str = Field(min_length=1, max_length=8192)
    mapping_config: dict[str, Any] = Field(default_factory=dict)


class TargetUpdateRequest(_RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    endpoint_url: str | None = Field(default=None, min_length=1, max_length=2048)
    credential: str | None = Field(default=None, min_length=1, max_length=8192)
    mapping_config: dict[str, Any] | None = None


class TargetOut(BaseModel):
    id: uuid.UUID
    name: str
    target_type: VectorTargetType
    endpoint_redacted: str
    mapping_config: dict[str, Any]
    credential_configured: bool
    credential_key_id: str
    credential_version: int
    allowlist_decision: str | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Manifests and snapshots (wave B slice 3)
# --------------------------------------------------------------------------


class SignatureOut(BaseModel):
    key_id: str
    signed_at: datetime
    issuer: str | None


class ManifestOut(BaseModel):
    id: uuid.UUID
    build_id: uuid.UUID | None
    namespace: str
    manifest_hash: str
    status: ManifestStatus
    source_count: int | None
    chunk_count: int | None
    embedding_count: int | None
    signed: bool
    signatures: list[SignatureOut]
    created_at: datetime


class ManifestVerifyResponse(BaseModel):
    hash_valid: bool
    overall: str
    signatures: list[dict[str, str]]


class SnapshotOut(BaseModel):
    id: uuid.UUID
    target_id: uuid.UUID
    status: SnapshotStatus
    point_count: int | None
    content_hash: str | None
    schema_hash: str | None
    created_at: datetime


class SnapshotCreateResponse(BaseModel):
    snapshot: SnapshotOut
    job: JobOut


# --------------------------------------------------------------------------
# Audit (FR-143 groundwork)
# --------------------------------------------------------------------------


class AuditEventOut(BaseModel):
    id: uuid.UUID
    actor_type: str
    actor_id: str | None
    action: str
    entity_type: str | None
    entity_id: str | None
    result: str
    request_id: str | None
    metadata: dict[str, Any] | None
    created_at: datetime
