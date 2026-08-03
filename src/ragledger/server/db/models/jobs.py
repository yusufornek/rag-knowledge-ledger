"""`Job`: the DB source of truth for Dramatiq-orchestrated work, per PROJECT_SPEC.md section 21.

Section 21: "Dramatiq Redis broker, DB source of truth. Job message IDs
only. DB lease `FOR UPDATE SKIP LOCKED`." This row is what a worker
leases (`lease_owner`/`lease_expires_at`); the Redis message that
triggers a worker to look at it carries only this row's `id`, never a
payload duplicate. Job execution and the actual Dramatiq actors are a
later wave's concern -- this table only needs to exist and be lease-
queryable for that wave to build on.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, false
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ragledger.server.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from ragledger.server.db.models.enums import JobStatus, enum_values

__all__ = ["Job"]


class Job(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_workspace_status_created", "workspace_id", "status", "created_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        SqlEnum(JobStatus, name="job_status", native_enum=True, values_callable=enum_values),
        nullable=False,
        default=JobStatus.QUEUED,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    related_entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=false()
    )
    """Cooperative cancellation flag (section 21): an API `:cancel` sets it;
    a running handler polls it between units of work and aborts cleanly."""
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
