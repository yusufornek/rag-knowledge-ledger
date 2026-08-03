"""ACL and tenant assertion construction, per the design specification section 8.8/12.3
and FR-070..FR-076.

Canonical ACL entry grammar (section 12.3): ``PUBLIC``, ``USER:<id>``,
``GROUP:<id>``, ``ROLE:<name>``, ``ATTRIBUTE:<key>=<value>``. Entries
are set semantics (deduplicated) and canonically sorted -- order in the
manifest never carries meaning (FR-076). Deny entries are not supported
in v1 (section 12.3): `validate_acl_entry` raises
`UnsupportedAclEntryError` for any ``DENY:``-prefixed entry rather than
accepting and silently ignoring it.

Case folding of principal identifiers never happens unless a caller
explicitly opts in via `AclConfig.case_normalize` (the design specification
section 40: "ACL principals case sensitivity source-system-specific...
Default no lowercase email/group unless declared").

This module only ever builds *expected*-side assertions from
declarative configuration (source-URI-keyed entries or path-glob
rules); comparing them against an *observed* index payload is
reconciliation's job (M6), out of scope here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from typing import cast

from ragledger.core.canonical import JSONValue
from ragledger.core.hashing import hash_canonical
from ragledger.core.models import AclAssertion, TenantAssertion
from ragledger.governance.identity import derive_assertion_id

PUBLIC = "PUBLIC"

_USER_RE = re.compile(r"^USER:.+$")
_GROUP_RE = re.compile(r"^GROUP:.+$")
_ROLE_RE = re.compile(r"^ROLE:.+$")
_ATTRIBUTE_RE = re.compile(r"^ATTRIBUTE:[^=]+=.+$")
_DENY_RE = re.compile(r"^DENY:", re.IGNORECASE)


class AclValidationError(ValueError):
    """Raised for a malformed ACL entry."""


class UnsupportedAclEntryError(AclValidationError):
    """Raised for a deny-style entry, unsupported in v1 (section 12.3)."""


def validate_acl_entry(entry: str) -> None:
    if _DENY_RE.match(entry):
        raise UnsupportedAclEntryError(
            f"deny entries are not supported in v1 (design specification 12.3): {entry!r}"
        )
    if entry == PUBLIC:
        return
    for pattern in (_USER_RE, _GROUP_RE, _ROLE_RE, _ATTRIBUTE_RE):
        if pattern.match(entry):
            return
    raise AclValidationError(
        f"unrecognized ACL entry {entry!r}; expected PUBLIC, USER:<id>, GROUP:<id>, "
        "ROLE:<name>, or ATTRIBUTE:<key>=<value>"
    )


def normalize_acl_entries(entries: Iterable[str], *, case_normalize: bool = False) -> list[str]:
    """Validate, optionally case-fold, deduplicate, and canonically sort ACL entries."""
    normalized: set[str] = set()
    for entry in entries:
        validate_acl_entry(entry)
        normalized.add(entry.lower() if case_normalize else entry)
    return sorted(normalized)


@dataclass(frozen=True)
class AclPathRule:
    pattern: str
    entries: tuple[str, ...]


@dataclass(frozen=True)
class AclConfig:
    source_entries: dict[str, tuple[str, ...]] = field(default_factory=dict)
    path_rules: tuple[AclPathRule, ...] = ()
    case_normalize: bool = False


def expected_acl_entries(uri: str, config: AclConfig) -> list[str] | None:
    """Resolve the expected canonical ACL entry set for one source's URI.

    Returns `None` when no ACL policy applies to this source at all --
    distinct from an explicit, empty "no access" set -- so callers can
    tell "not configured" apart from "configured as empty" (FR-070).
    """
    direct = config.source_entries.get(uri)
    if direct is not None:
        return normalize_acl_entries(direct, case_normalize=config.case_normalize)
    for rule in config.path_rules:
        if fnmatch(uri, rule.pattern):
            return normalize_acl_entries(rule.entries, case_normalize=config.case_normalize)
    return None


def build_acl_assertion(
    subject_ref: str, entries: Sequence[str], created_at: datetime, *, case_normalize: bool = False
) -> AclAssertion:
    normalized = normalize_acl_entries(entries, case_normalize=case_normalize)
    acl_hash = hash_canonical(cast(JSONValue, normalized))
    assertion_id = derive_assertion_id("acl", subject_ref, acl_hash)
    return AclAssertion(
        id=assertion_id,
        subject_ref=subject_ref,
        created_at=created_at,
        acl_hash=acl_hash,
        entries=normalized,
    )


# --------------------------------------------------------------------------
# Tenant (FR-072)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantPathRule:
    pattern: str
    tenant_key: str
    tenant_value: str


@dataclass(frozen=True)
class TenantConfig:
    source_tenants: dict[str, tuple[str, str]] = field(default_factory=dict)
    path_rules: tuple[TenantPathRule, ...] = ()
    required: bool = False
    """Whether every discovered source is expected to resolve a tenant.
    Enforcing this as a pass/fail policy verdict is reconciliation's job
    (M6); this flag only controls whether `ragledger.pipeline.build`
    records a `TENANT_REQUIRED_BUT_MISSING` warning when a source has no
    resolvable tenant, per FR-072's "mandatory/optional policy"."""


def expected_tenant(uri: str, config: TenantConfig) -> tuple[str, str] | None:
    """Resolve the expected `(tenant_key, tenant_value)` pair for one source's URI."""
    direct = config.source_tenants.get(uri)
    if direct is not None:
        return direct
    for rule in config.path_rules:
        if fnmatch(uri, rule.pattern):
            return rule.tenant_key, rule.tenant_value
    return None


def build_tenant_assertion(
    subject_ref: str, tenant_key: str, tenant_value: str, created_at: datetime
) -> TenantAssertion:
    tenant_hash = hash_canonical({"key": tenant_key, "value": tenant_value})
    assertion_id = derive_assertion_id("tnt", subject_ref, tenant_key, tenant_value)
    return TenantAssertion(
        id=assertion_id,
        subject_ref=subject_ref,
        created_at=created_at,
        tenant_hash=tenant_hash,
        tenant_key=tenant_key,
        tenant_value=tenant_value,
    )
