"""`Policy`, `PolicyRevision`, `PolicyEvaluation`, per PROJECT_SPEC.md section 15.1.

"Policy, PolicyRevision, PolicyEvaluation | Gate." A `Policy` is a
named, mutable pointer; each edit creates a new, immutable
`PolicyRevision` (never an update in place, per section 15.3); a
`PolicyEvaluation` records the pass/warn/fail outcome of running one
revision against one `Reconciliation`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Enum as SqlEnum
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ragledger.server.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from ragledger.server.db.models.enums import PolicyEvaluationResult, enum_values

__all__ = ["Policy", "PolicyEvaluation", "PolicyRevision"]


class Policy(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class PolicyRevision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable once written; a new edit is always a new row with an incremented number."""

    __tablename__ = "policy_revisions"
    __table_args__ = (
        UniqueConstraint("policy_id", "revision_number"),
        UniqueConstraint("workspace_id", "config_hash"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rules_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class PolicyEvaluation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable once written: the outcome of gating one reconciliation against one revision."""

    __tablename__ = "policy_evaluations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), nullable=False
    )
    policy_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    result: Mapped[PolicyEvaluationResult] = mapped_column(
        SqlEnum(
            PolicyEvaluationResult,
            name="policy_evaluation_result",
            native_enum=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
