"""ORM models for the section 15.1 entities this wave covers.

Importing this package registers every model class on `Base`'s
declarative registry, which Alembic's `env.py` relies on for
`target_metadata`. Import this module (or any name from it) before
constructing an engine/session anywhere that needs the full schema --
importing an individual model submodule alone does not guarantee the
others (and their foreign keys) are registered yet.
"""

from __future__ import annotations

from ragledger.server.db.models.audit import AuditEvent
from ragledger.server.db.models.builds import Build
from ragledger.server.db.models.enums import (
    BuildState,
    FindingSeverity,
    JobStatus,
    ManifestStatus,
    MembershipRole,
    PolicyEvaluationResult,
    ReconciliationState,
    SnapshotStatus,
    VectorTargetType,
)
from ragledger.server.db.models.jobs import Job
from ragledger.server.db.models.manifests import Manifest, ManifestSignature
from ragledger.server.db.models.policy import Policy, PolicyEvaluation, PolicyRevision
from ragledger.server.db.models.reconciliation import Finding, LineageIndex, Reconciliation
from ragledger.server.db.models.sources import (
    PipelineConfig,
    SourceAsset,
    SourceCollection,
    SourceVersion,
)
from ragledger.server.db.models.targets import InventorySnapshot, VectorTarget
from ragledger.server.db.models.workspace import ApiToken, Membership, User, Workspace

__all__ = [
    "ApiToken",
    "AuditEvent",
    "Build",
    "BuildState",
    "Finding",
    "FindingSeverity",
    "InventorySnapshot",
    "Job",
    "JobStatus",
    "LineageIndex",
    "Manifest",
    "ManifestSignature",
    "ManifestStatus",
    "Membership",
    "MembershipRole",
    "PipelineConfig",
    "Policy",
    "PolicyEvaluation",
    "PolicyEvaluationResult",
    "PolicyRevision",
    "Reconciliation",
    "ReconciliationState",
    "SnapshotStatus",
    "SourceAsset",
    "SourceCollection",
    "SourceVersion",
    "User",
    "VectorTarget",
    "VectorTargetType",
    "Workspace",
]
