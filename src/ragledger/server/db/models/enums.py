"""Bounded-vocabulary columns modeled as Python `StrEnum` types.

Each enum here backs a native Postgres `ENUM` column (via SQLAlchemy's
`Enum` type), so an invalid value is rejected by the database itself,
not only by application code.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

__all__ = [
    "BuildState",
    "FindingSeverity",
    "JobStatus",
    "ManifestStatus",
    "MembershipRole",
    "PolicyEvaluationResult",
    "ReconciliationState",
    "SnapshotStatus",
    "VectorTargetType",
    "enum_values",
]

_E = TypeVar("_E", bound=StrEnum)


def enum_values(enum_cls: type[_E]) -> list[str]:
    """`values_callable` for `sqlalchemy.Enum`: store each member's `.value`, not its `.name`.

    SQLAlchemy's `Enum` type defaults to the Python enum member *name*
    (`"PENDING"`) rather than its `.value` (`"pending"`) unless given
    this. Every `sqlalchemy.Enum(...)` column in this package's models
    passes `values_callable=enum_values` so the stored strings match
    what application code actually compares against (a `StrEnum`
    member equals its lowercase value, not its uppercase name).
    """
    return [member.value for member in enum_cls]


class MembershipRole(StrEnum):
    """the design specification FR-001: "workspace ve roles owner/editor/viewer"."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class ManifestStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class VectorTargetType(StrEnum):
    QDRANT = "qdrant"
    PGVECTOR = "pgvector"


class SnapshotStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


class BuildState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReconciliationState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class PolicyEvaluationResult(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
