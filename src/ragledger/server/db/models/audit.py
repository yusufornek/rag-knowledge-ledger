"""`AuditEvent`, per PROJECT_SPEC.md section 15.1/15.3/36.4.

Append-only: no update or delete path is exposed anywhere in this
package (`ragledger.server.audit.AuditLog.record` only ever inserts).
Section 15.3 asks for "monthly partition readiness"; this wave indexes
`(workspace_id, created_at)` so a monthly-partitioned table (native
Postgres declarative partitioning) can be introduced later without an
application-visible change, but does not itself declare the table
partitioned -- see `IMPLEMENTATION_STATUS.md` for that gap.

`metadata_json` never carries a raw secret or PII value; see
`ragledger.server.audit` for what is and is not allowed into it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ragledger.server.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["AuditEvent"]


class AuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_workspace_created", "workspace_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
