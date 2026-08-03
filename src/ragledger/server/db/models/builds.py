"""The `Build` entity: a pipeline job, per the design specification section 15.1/36.1/36.4.

"builds: config/source snapshot/state/counters." A build is the unit
that turns a `SourceCollection` plus a `PipelineConfig` revision into a
`Manifest` (see `ragledger.server.db.models.manifests.Manifest`, the
ER diagram's `BUILD ||--|| MANIFEST : produces`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ragledger.server.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from ragledger.server.db.models.enums import BuildState, enum_values

__all__ = ["Build"]


class Build(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "builds"
    __table_args__ = (Index("ix_builds_workspace_created_id", "workspace_id", "created_at", "id"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_collections.id", ondelete="RESTRICT"), nullable=False
    )
    pipeline_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_configs.id", ondelete="RESTRICT"), nullable=False
    )
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[BuildState] = mapped_column(
        SqlEnum(BuildState, name="build_state", native_enum=True, values_callable=enum_values),
        nullable=False,
        default=BuildState.PENDING,
    )
    counters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
