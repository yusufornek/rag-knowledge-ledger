"""Finding taxonomy tests: every code constructible, fingerprint stability,
severity-override guard, and the ACL principal masking canary.
"""

from __future__ import annotations

import pytest

from ragledger.reconcile.taxonomy import (
    DEFAULT_SEVERITY,
    FindingCode,
    FindingSeverity,
    SeverityOverrideError,
    build_finding,
    mask_acl_entries,
    mask_acl_entry,
)

ALL_CODES = list(FindingCode)


def test_taxonomy_has_at_least_15_codes_per_dod() -> None:
    assert len(ALL_CODES) >= 15


def test_taxonomy_matches_policy_schema_enum() -> None:
    """`FindingCode` must exactly match `policy-v1.schema.json`'s
    `$defs.taxonomyCode` enum -- the policy schema is the other place this
    exact code list is transcribed."""
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[2] / "docs" / "spec" / "policy-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_codes = set(schema["$defs"]["taxonomyCode"]["enum"])
    assert {code.value for code in FindingCode} == schema_codes


@pytest.mark.parametrize("code", ALL_CODES)
def test_every_finding_type_is_constructible(code: FindingCode) -> None:
    finding = build_finding(
        code=code,
        target="tgt",
        scope="scope-a",
        subject_id="subject-1",
        affected_field="some_field",
        evidence={"note": "synthetic"},
    )
    assert finding.code is code
    assert finding.severity is DEFAULT_SEVERITY[code]
    assert finding.fingerprint
    assert finding.locator.target == "tgt"
    assert finding.locator.scope == "scope-a"


def test_fingerprint_stable_for_same_logical_finding() -> None:
    kwargs = {
        "code": FindingCode.MISSING_IN_INDEX,
        "target": "tgt",
        "scope": "scope-a",
        "subject_id": "idx_abc",
        "affected_field": "point_id",
    }
    first = build_finding(**kwargs, evidence={"anything": "here"})
    second = build_finding(
        **kwargs, evidence={"different": "evidence dict"}, detail="different detail text"
    )
    assert first.fingerprint == second.fingerprint


@pytest.mark.parametrize(
    "field_to_change",
    ["code", "target", "scope", "subject_id", "affected_field"],
)
def test_fingerprint_changes_when_any_anchor_field_changes(field_to_change: str) -> None:
    base_kwargs: dict[str, object] = {
        "code": FindingCode.MISSING_IN_INDEX,
        "target": "tgt",
        "scope": "scope-a",
        "subject_id": "idx_abc",
        "affected_field": "point_id",
    }
    baseline = build_finding(**base_kwargs)  # type: ignore[arg-type]

    changed_kwargs = dict(base_kwargs)
    if field_to_change == "code":
        changed_kwargs["code"] = FindingCode.ORPHAN_IN_INDEX
    else:
        changed_kwargs[field_to_change] = str(changed_kwargs[field_to_change]) + "-changed"
    changed = build_finding(**changed_kwargs)  # type: ignore[arg-type]

    assert baseline.fingerprint != changed.fingerprint


def test_fingerprint_excludes_timestamp_and_message_by_construction() -> None:
    """There is no timestamp parameter on `build_finding` at all: the
    fingerprint recipe (section 14.5) structurally cannot include one."""
    first = build_finding(
        code=FindingCode.STALE_SOURCE,
        target="tgt",
        scope="scope-a",
        subject_id="idx_abc",
        affected_field="source_version_id",
        detail="observed at run 1",
    )
    second = build_finding(
        code=FindingCode.STALE_SOURCE,
        target="tgt",
        scope="scope-a",
        subject_id="idx_abc",
        affected_field="source_version_id",
        detail="observed at run 2, totally different message",
    )
    assert first.fingerprint == second.fingerprint


def test_severity_override_requires_reason_for_critical_default() -> None:
    assert DEFAULT_SEVERITY[FindingCode.ACL_MISSING] is FindingSeverity.CRITICAL
    with pytest.raises(SeverityOverrideError):
        build_finding(
            code=FindingCode.ACL_MISSING,
            target="tgt",
            scope="scope-a",
            subject_id="idx_abc",
            affected_field="acl",
            severity=FindingSeverity.LOW,
        )


def test_severity_override_allowed_with_explicit_reason() -> None:
    finding = build_finding(
        code=FindingCode.ACL_MISSING,
        target="tgt",
        scope="scope-a",
        subject_id="idx_abc",
        affected_field="acl",
        severity=FindingSeverity.LOW,
        severity_override_reason="known false positive for this connector",
    )
    assert finding.severity is FindingSeverity.LOW


def test_severity_override_upgrade_never_needs_a_reason() -> None:
    finding = build_finding(
        code=FindingCode.LICENSE_UNKNOWN,
        target="tgt",
        scope="scope-a",
        subject_id="idx_abc",
        affected_field="license",
        severity=FindingSeverity.HIGH,
    )
    assert finding.severity is FindingSeverity.HIGH


# --------------------------------------------------------------------------
# ACL principal masking (acceptance scenario D)
# --------------------------------------------------------------------------


def test_public_acl_entry_is_not_masked() -> None:
    assert mask_acl_entry("PUBLIC") == "PUBLIC"


def test_typed_acl_entry_never_reveals_raw_identifier() -> None:
    raw = "USER:jane.doe@example.com"
    masked = mask_acl_entry(raw)
    assert "jane.doe@example.com" not in masked
    assert masked.startswith("USER:masked:")


def test_acl_masking_is_deterministic_and_groups_repeats() -> None:
    first = mask_acl_entry("GROUP:finance")
    second = mask_acl_entry("GROUP:finance")
    assert first == second


def test_acl_masking_with_workspace_secret_still_hides_raw_value() -> None:
    masked = mask_acl_entry("USER:jane.doe@example.com", workspace_secret=b"workspace-secret-key")
    assert "jane.doe@example.com" not in masked
    unkeyed = mask_acl_entry("USER:jane.doe@example.com")
    assert masked != unkeyed  # keyed and unkeyed digests differ


def test_mask_acl_entries_masks_every_entry_and_sorts() -> None:
    masked = mask_acl_entries(["GROUP:finance", "PUBLIC", "USER:alice@example.com"])
    assert "alice@example.com" not in " ".join(masked)
    assert "PUBLIC" in masked
    assert masked == sorted(masked)
