"""Reconciliation finding taxonomy, per the design specification section 9 and 14.5.

The 23 taxonomy codes below, and their default severities, are transcribed
directly from section 9's table -- the same table
`docs/spec/policy-v1.schema.json`'s `$defs.taxonomyCode` enum already
enumerates (this module's `FindingCode` values match that enum
member-for-member).

Section 9's severity-override rule ("Severity policy ile override edilebilir;
critical defaultlar güvenlik nedeniyle explicit override reason gerektirir")
is enforced by `build_finding`: silently downgrading a `critical`-by-default
code is a programming error, not a policy decision, so it raises unless the
caller passes `severity_override_reason`.

No finding built here ever carries a raw PII value or a raw ACL principal
identifier (the HARD RULES this release was scoped under): PII evidence
reuses `ragledger.governance.pii.PiiFinding`'s already-masked shape
(`masked_preview`/`value_hmac`, never the raw match), and `mask_acl_entry`
hashes every typed ACL entry's identifier component before it can reach a
`Finding.evidence` dict.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from cryptography.hazmat.primitives import hashes as crypto_hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import Field

from ragledger.core.models import PointId, RagledgerModel
from ragledger.governance.identity import derive_assertion_id

__all__ = [
    "DEFAULT_SEVERITY",
    "AffectedLineage",
    "Finding",
    "FindingCode",
    "FindingSeverity",
    "Locator",
    "build_finding",
    "compute_fingerprint",
    "mask_acl_entries",
    "mask_acl_entry",
]


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingCode(StrEnum):
    """Section 9's reconciliation taxonomy (23 codes)."""

    MISSING_IN_INDEX = "MISSING_IN_INDEX"
    ORPHAN_IN_INDEX = "ORPHAN_IN_INDEX"
    STALE_SOURCE = "STALE_SOURCE"
    STALE_PARSE = "STALE_PARSE"
    STALE_CHUNKING = "STALE_CHUNKING"
    EMBEDDING_MODEL_MISMATCH = "EMBEDDING_MODEL_MISMATCH"
    EMBEDDING_DIMENSION_MISMATCH = "EMBEDDING_DIMENSION_MISMATCH"
    VECTOR_HASH_MISMATCH = "VECTOR_HASH_MISMATCH"
    PAYLOAD_DRIFT = "PAYLOAD_DRIFT"
    SOURCE_METADATA_MISSING = "SOURCE_METADATA_MISSING"
    DUPLICATE_POINT_ID = "DUPLICATE_POINT_ID"
    DUPLICATE_CONTENT = "DUPLICATE_CONTENT"
    ACL_MISSING = "ACL_MISSING"
    ACL_BROADER_THAN_SOURCE = "ACL_BROADER_THAN_SOURCE"
    ACL_MISMATCH = "ACL_MISMATCH"
    TENANT_MISSING = "TENANT_MISSING"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    PII_POLICY_VIOLATION = "PII_POLICY_VIOLATION"
    LICENSE_UNKNOWN = "LICENSE_UNKNOWN"
    LICENSE_POLICY_VIOLATION = "LICENSE_POLICY_VIOLATION"
    UNVERIFIABLE_POINT = "UNVERIFIABLE_POINT"
    TARGET_SCHEMA_DRIFT = "TARGET_SCHEMA_DRIFT"
    MANIFEST_INCOMPLETE = "MANIFEST_INCOMPLETE"
    SNAPSHOT_INCOMPLETE = "SNAPSHOT_INCOMPLETE"


DEFAULT_SEVERITY: dict[FindingCode, FindingSeverity] = {
    FindingCode.MISSING_IN_INDEX: FindingSeverity.HIGH,
    FindingCode.ORPHAN_IN_INDEX: FindingSeverity.HIGH,
    FindingCode.STALE_SOURCE: FindingSeverity.HIGH,
    FindingCode.STALE_PARSE: FindingSeverity.MEDIUM,
    FindingCode.STALE_CHUNKING: FindingSeverity.HIGH,
    FindingCode.EMBEDDING_MODEL_MISMATCH: FindingSeverity.HIGH,
    FindingCode.EMBEDDING_DIMENSION_MISMATCH: FindingSeverity.CRITICAL,
    FindingCode.VECTOR_HASH_MISMATCH: FindingSeverity.HIGH,
    FindingCode.PAYLOAD_DRIFT: FindingSeverity.MEDIUM,
    FindingCode.SOURCE_METADATA_MISSING: FindingSeverity.MEDIUM,
    FindingCode.DUPLICATE_POINT_ID: FindingSeverity.CRITICAL,
    FindingCode.DUPLICATE_CONTENT: FindingSeverity.MEDIUM,
    FindingCode.ACL_MISSING: FindingSeverity.CRITICAL,
    FindingCode.ACL_BROADER_THAN_SOURCE: FindingSeverity.CRITICAL,
    FindingCode.ACL_MISMATCH: FindingSeverity.HIGH,
    FindingCode.TENANT_MISSING: FindingSeverity.CRITICAL,
    FindingCode.TENANT_MISMATCH: FindingSeverity.CRITICAL,
    # Section 9's table lists "High/Critical"; this module defaults to HIGH
    # and lets callers (ragledger.reconcile.engine) opt into CRITICAL for
    # very-high-confidence findings via the normal (non-downgrade) override
    # path -- see this module's docstring.
    FindingCode.PII_POLICY_VIOLATION: FindingSeverity.HIGH,
    FindingCode.LICENSE_UNKNOWN: FindingSeverity.MEDIUM,
    FindingCode.LICENSE_POLICY_VIOLATION: FindingSeverity.HIGH,
    FindingCode.UNVERIFIABLE_POINT: FindingSeverity.MEDIUM,
    FindingCode.TARGET_SCHEMA_DRIFT: FindingSeverity.HIGH,
    FindingCode.MANIFEST_INCOMPLETE: FindingSeverity.HIGH,
    FindingCode.SNAPSHOT_INCOMPLETE: FindingSeverity.HIGH,
}


class SeverityOverrideError(ValueError):
    """Raised when a caller silently downgrades a critical-by-default finding."""


def compute_fingerprint(
    code: FindingCode, target: str, scope: str, subject_id: str, affected_field: str
) -> str:
    """Section 14.5's finding fingerprint: taxonomy code + target id +
    normalized point/binding id + affected field.

    Deliberately excludes any timestamp or free-text message -- the whole
    point is that the same logical finding fingerprints identically across
    reruns, so a report diff can tell "new" from "persistent" apart from
    "resolved" (FR-126).
    """
    return derive_assertion_id("fnd", code.value, target, scope, subject_id, affected_field)


class Locator(RagledgerModel):
    """Target/collection/point locator for one finding (section 9's evidence shape)."""

    target: str
    scope: str
    point_id: PointId | None = None
    binding_id: str | None = None


class AffectedLineage(RagledgerModel):
    source_id: str | None = None
    source_version_id: str | None = None
    chunk_id: str | None = None
    embedding_id: str | None = None


class Finding(RagledgerModel):
    """One reconciliation finding.

    `evidence` is a JSON-safe, already-masked dict -- callers building
    ACL/PII evidence must mask before constructing (see `mask_acl_entry`;
    `ragledger.governance.pii.PiiFinding` is masked by construction).
    `match_level`/`confidence` are only set for findings anchored to a
    `ragledger.reconcile.matching.MatchedPair` (or its heuristic
    suggestions); structural findings (schema/manifest/snapshot) leave them
    `None`.
    """

    fingerprint: str
    code: FindingCode
    severity: FindingSeverity
    locator: Locator
    affected_lineage: AffectedLineage = Field(default_factory=AffectedLineage)
    evidence: dict[str, Any] = Field(default_factory=dict)
    match_level: int | None = None
    confidence: str | None = None
    detail: str | None = None


def build_finding(
    *,
    code: FindingCode,
    target: str,
    scope: str,
    subject_id: str,
    affected_field: str,
    severity: FindingSeverity | None = None,
    severity_override_reason: str | None = None,
    point_id: PointId | None = None,
    binding_id: str | None = None,
    affected_lineage: AffectedLineage | None = None,
    evidence: Mapping[str, Any] | None = None,
    match_level: int | None = None,
    confidence: str | None = None,
    detail: str | None = None,
) -> Finding:
    """Construct one `Finding`, computing its fingerprint and default severity.

    `subject_id` and `affected_field` are the fingerprint's stability
    anchors (section 14.5): `subject_id` should be a binding id, a
    normalized point id, or another stable per-finding subject, and
    `affected_field` should name the specific comparison that produced this
    finding (for example `"point_id"`, `"source_version_id"`, `"acl"`).
    """
    default_severity = DEFAULT_SEVERITY[code]
    if severity is None:
        resolved_severity = default_severity
    elif (
        default_severity is FindingSeverity.CRITICAL
        and severity is not FindingSeverity.CRITICAL
        and severity_override_reason is None
    ):
        raise SeverityOverrideError(
            f"{code.value} defaults to critical severity; downgrading to "
            f"{severity.value} requires an explicit severity_override_reason "
            "(the design specification section 9: 'critical defaultlar güvenlik "
            "nedeniyle explicit override reason gerektirir')"
        )
    else:
        resolved_severity = severity

    return Finding(
        fingerprint=compute_fingerprint(code, target, scope, subject_id, affected_field),
        code=code,
        severity=resolved_severity,
        locator=Locator(target=target, scope=scope, point_id=point_id, binding_id=binding_id),
        affected_lineage=affected_lineage or AffectedLineage(),
        evidence=dict(evidence or {}),
        match_level=match_level,
        confidence=confidence,
        detail=detail,
    )


# --------------------------------------------------------------------------
# ACL principal masking (acceptance scenario D)
# --------------------------------------------------------------------------

_PRINCIPAL_HMAC_DOMAIN_INFO = b"ragledger-reconcile-principal-hmac-v1"
"""Domain-separated from `ragledger.governance.pii`'s PII value-HMAC info
string: this masks a different kind of value (ACL principal identifiers, not
PII matches) and must never be derivable from, or confusable with, that
other HMAC key even when the same workspace secret is supplied to both."""


def _derive_principal_hmac_key(workspace_secret: bytes) -> bytes:
    kdf = HKDF(
        algorithm=crypto_hashes.SHA256(), length=32, salt=None, info=_PRINCIPAL_HMAC_DOMAIN_INFO
    )
    return kdf.derive(workspace_secret)


def mask_acl_entry(entry: str, workspace_secret: bytes | None = None) -> str:
    """Mask one ACL entry for finding evidence -- never the raw principal.

    `PUBLIC` carries no principal identity and passes through unmasked.
    Typed entries (`USER:..`, `GROUP:..`, `ROLE:..`, `ATTRIBUTE:..=..`) keep
    their kind prefix (useful for remediation/severity reasoning) but
    replace the identifier with a stable digest, so the same principal
    always masks to the same value (grouping duplicates in a report) without
    the raw identifier ever appearing. HMAC-keyed (workspace-scoped, HKDF
    domain-separated) when a workspace secret is supplied, else a plain
    SHA-256 -- ACL principals are lower-entropy-adjacent identifiers, not
    high-value secrets, so an unkeyed digest is an acceptable default, but a
    keyed one is preferred when available (mirrors
    `ragledger.governance.pii.compute_value_hmac`'s reasoning).
    """
    if entry == "PUBLIC":
        return entry
    kind, separator, rest = entry.partition(":")
    if not separator:
        rest = entry
        kind = "PRINCIPAL"
    if workspace_secret:
        key = _derive_principal_hmac_key(workspace_secret)
        digest = hmac.new(key, rest.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    else:
        digest = hashlib.sha256(rest.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:masked:{digest}"


def mask_acl_entries(entries: Iterable[str], workspace_secret: bytes | None = None) -> list[str]:
    return sorted(mask_acl_entry(entry, workspace_secret) for entry in entries)
