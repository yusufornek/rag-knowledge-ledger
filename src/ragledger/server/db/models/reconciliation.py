"""`Reconciliation`, `Finding`, and `lineage_index`, per PROJECT_SPEC.md section 15.1/36.1/36.4.

`Finding` stores only a bounded, searchable summary of each drift/
governance issue (code, severity, source/chunk/point hashes,
fingerprint, and *bounded* expected/observed evidence); the full
finding record is an NDJSON artifact shard referenced by
`artifact_ref`, per section 36.1: "Full record NDJSON artifact."
`lineage_index` is the drill-down table section 36.1 names directly:
it maps a portable id (source/version/parse-run/chunk/embedding/index-
binding) to the artifact shard and byte range or Parquet row group
that holds its full record, so a web UI (a later milestone) can jump
straight to the right offset instead of scanning a shard.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ragledger.server.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from ragledger.server.db.models.enums import FindingSeverity, ReconciliationState, enum_values

__all__ = ["Finding", "LineageIndex", "Reconciliation"]


class Reconciliation(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "reconciliations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    manifest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("manifests.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inventory_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    policy_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policy_revisions.id", ondelete="SET NULL"), nullable=True
    )
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[ReconciliationState] = mapped_column(
        SqlEnum(
            ReconciliationState,
            name="reconciliation_state",
            native_enum=True,
            values_callable=enum_values,
        ),
        nullable=False,
        default=ReconciliationState.PENDING,
    )
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class Finding(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable once written. Full-fidelity evidence lives in the `artifact_ref` NDJSON shard."""

    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("reconciliation_id", "fingerprint"),
        Index("ix_findings_reconciliation_severity_code", "reconciliation_id", "severity", "code"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[FindingSeverity] = mapped_column(
        SqlEnum(
            FindingSeverity, name="finding_severity", native_enum=True, values_callable=enum_values
        ),
        nullable=False,
    )
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    chunk_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    point_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    expected_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    observed_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class LineageIndex(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Drill-down: portable id -> the artifact shard/byte-range or row-group holding its record."""

    __tablename__ = "lineage_index"
    __table_args__ = (
        Index(
            "ix_lineage_index_workspace_type_id", "workspace_id", "portable_id_type", "portable_id"
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    portable_id_type: Mapped[str] = mapped_column(String(32), nullable=False)
    portable_id: Mapped[str] = mapped_column(String(80), nullable=False)
    manifest_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("manifests.id", ondelete="CASCADE"), nullable=True
    )
    reconciliation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), nullable=True
    )
    artifact_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    byte_range_start: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    byte_range_end: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    row_group: Mapped[int | None] = mapped_column(Integer, nullable=True)
