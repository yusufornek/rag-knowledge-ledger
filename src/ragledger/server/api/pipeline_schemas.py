"""Request/response DTOs for source collections, pipeline configs, builds, and jobs.

Same posture as `ragledger.server.api.schemas`: requests are
``extra="forbid"``, responses never carry secret material.

`PipelineConfigBody` reuses the CLI's own strict config sub-models
(`ragledger.cli._config`): a server pipeline config is exactly a
`ragledger.yml` minus its `namespace`/`sources` section (those live on
the `SourceCollection` the build pairs it with), so the two surfaces
cannot drift apart.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ragledger.cli._config import (
    ChunkerConfig,
    EmbeddingConfig,
    GovernanceConfig,
    ManifestSectionConfig,
    ParserConfig,
)
from ragledger.server.db.models.enums import (
    BuildState,
    FindingSeverity,
    JobStatus,
    ReconciliationState,
)

__all__ = [
    "BuildCreateRequest",
    "BuildOut",
    "FindingOut",
    "JobOut",
    "PipelineConfigBody",
    "PipelineConfigCreateRequest",
    "PipelineConfigOut",
    "PolicyCreateRequest",
    "PolicyOut",
    "PolicyRevisionCreateRequest",
    "PolicyRevisionOut",
    "ReconciliationCreateRequest",
    "ReconciliationCreateResponse",
    "ReconciliationOut",
    "SourceAssetOut",
    "SourceCollectionCreateRequest",
    "SourceCollectionOut",
    "SourceVersionOut",
]


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Source collections
# --------------------------------------------------------------------------


class SourceCollectionCreateRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=255)
    namespace: str = Field(min_length=1, max_length=255)
    root: str = Field(min_length=1, max_length=4096)

    @field_validator("root")
    @classmethod
    def _validate_root_shape(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("root must be an absolute path")
        return value


class SourceCollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    namespace: str
    root: str
    created_at: datetime


class SourceAssetOut(BaseModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    portable_id: str
    uri: str
    status: str


class SourceVersionOut(BaseModel):
    id: uuid.UUID
    portable_id: str
    content_hash: str
    media_type: str
    size_bytes: int
    created_at: datetime


# --------------------------------------------------------------------------
# Pipeline configs
# --------------------------------------------------------------------------


class PipelineConfigBody(_RequestModel):
    """A `ragledger.yml` without `namespace`/`sources`: the reusable pipeline half."""

    version: int = 1
    parser: ParserConfig = Field(default_factory=ParserConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    manifest: ManifestSectionConfig = Field(default_factory=ManifestSectionConfig)


class PipelineConfigCreateRequest(_RequestModel):
    config: PipelineConfigBody


class PipelineConfigOut(BaseModel):
    id: uuid.UUID
    config_hash: str
    config: dict[str, Any]
    created_at: datetime


# --------------------------------------------------------------------------
# Builds and jobs
# --------------------------------------------------------------------------


class BuildCreateRequest(_RequestModel):
    source_collection_id: uuid.UUID
    pipeline_config_id: uuid.UUID
    epoch: int | None = Field(default=None, ge=0)
    """Optional reproducible-build epoch (seconds); same semantics as the
    CLI's ``--epoch``/``SOURCE_DATE_EPOCH``."""


class BuildOut(BaseModel):
    id: uuid.UUID
    source_collection_id: uuid.UUID
    pipeline_config_id: uuid.UUID
    state: BuildState
    counters: dict[str, Any]
    job_id: uuid.UUID | None
    manifest_id: uuid.UUID | None
    manifest_hash: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobOut(BaseModel):
    id: uuid.UUID
    job_type: str
    status: JobStatus
    attempt_count: int
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


# --------------------------------------------------------------------------
# Policies, reconciliations, findings (wave B slice 4)
# --------------------------------------------------------------------------


class PolicyCreateRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=255)
    document: dict[str, Any]


class PolicyRevisionCreateRequest(_RequestModel):
    document: dict[str, Any]


class PolicyRevisionOut(BaseModel):
    id: uuid.UUID
    revision_number: int
    config_hash: str
    document: dict[str, Any]
    created_at: datetime


class PolicyOut(BaseModel):
    id: uuid.UUID
    name: str
    latest_revision: PolicyRevisionOut | None
    created_at: datetime


class ReconciliationCreateRequest(_RequestModel):
    manifest_id: uuid.UUID
    snapshot_id: uuid.UUID
    policy_id: uuid.UUID | None = None


class ReconciliationOut(BaseModel):
    id: uuid.UUID
    manifest_id: uuid.UUID
    snapshot_id: uuid.UUID
    policy_revision_id: uuid.UUID | None
    state: ReconciliationState
    summary: dict[str, Any] | None
    finding_count: int
    job_id: uuid.UUID | None
    created_at: datetime


class ReconciliationCreateResponse(BaseModel):
    reconciliation: ReconciliationOut
    job: JobOut


class FindingOut(BaseModel):
    id: uuid.UUID
    fingerprint: str
    code: str
    severity: FindingSeverity
    source_hash: str | None
    chunk_hash: str | None
    point_hash: str | None
    expected_evidence: dict[str, Any] | None
    observed_evidence: dict[str, Any] | None
    created_at: datetime
