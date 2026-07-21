"""Small-data reconciliation engine tests, per PROJECT_SPEC.md section 14."""

from __future__ import annotations

import pytest

from ragledger.reconcile.engine import reconcile_small_data
from ragledger.reconcile.policy import (
    FindingsPolicy,
    LicensesPolicy,
    PiiPolicy,
    PolicyDocument,
    Requirements,
)
from ragledger.reconcile.taxonomy import FindingCode
from tests.reconcile.builders import list_connector, make_scenario


def _bare_policy(**overrides: object) -> PolicyDocument:
    base: dict[str, object] = {
        "name": "test-policy",
        "requirements": Requirements(),
        "findings": FindingsPolicy(fail_on_severity=["critical"]),
    }
    base.update(overrides)
    return PolicyDocument(**base)


def test_clean_matched_point_produces_no_findings() -> None:
    scenario = make_scenario()
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    assert result.findings == []
    assert result.summary.matched_points == 1
    assert result.ratios.missing_ratio == 0.0
    assert result.ratios.orphan_ratio == 0.0


def test_missing_point_reports_missing_in_index() -> None:
    scenario = make_scenario()
    connector = list_connector([], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    codes = [f.code for f in result.findings]
    assert codes == [FindingCode.MISSING_IN_INDEX]
    assert result.ratios.missing_ratio == 1.0


def test_extra_point_reports_orphan_in_index() -> None:
    scenario = make_scenario()
    extra = scenario.matching_point.model_copy(
        update={
            "point_id": "extra-point",
            "embedding_id": None,
            "chunk_id": None,
            "source_id": None,
            "source_version_id": None,
            "payload_projection": {},
            "acl": None,
            "tenant": None,
        }
    )
    connector = list_connector([scenario.matching_point, extra], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    codes = {f.code for f in result.findings}
    assert FindingCode.UNVERIFIABLE_POINT in codes
    assert result.ratios.orphan_ratio == 0.5


def test_stale_source_when_observed_points_to_old_source_version() -> None:
    scenario = make_scenario()
    stale_point = scenario.matching_point.model_copy(
        update={"source_version_id": "ver_old_stale_version"}
    )
    connector = list_connector([stale_point], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    codes = [f.code for f in result.findings]
    assert FindingCode.STALE_SOURCE in codes
    assert result.ratios.stale_ratio == 1.0


def test_acl_broader_than_source_is_critical_and_masked() -> None:
    """Acceptance scenario D: expected group:finance, observed public."""
    scenario = make_scenario(acl_entries=("GROUP:finance",))
    leaked_point = scenario.matching_point.model_copy(update={"acl": ["PUBLIC"]})
    connector = list_connector([leaked_point], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    acl_findings = [f for f in result.findings if f.code is FindingCode.ACL_BROADER_THAN_SOURCE]
    assert len(acl_findings) == 1
    finding = acl_findings[0]
    assert finding.severity.value == "critical"
    evidence_text = str(finding.evidence)
    assert "finance" not in evidence_text  # raw principal never present


def test_tenant_mismatch_detected() -> None:
    scenario = make_scenario(tenant_value="tenant-a")
    wrong_tenant_point = scenario.matching_point.model_copy(update={"tenant": "tenant-b"})
    connector = list_connector([wrong_tenant_point], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    codes = [f.code for f in result.findings]
    assert FindingCode.TENANT_MISMATCH in codes


def test_embedding_dimension_mismatch_short_circuits_before_full_scan() -> None:
    """Acceptance scenario C: collection is 768-d, manifest expects 1024-d."""
    scenario = make_scenario(dimension=1024)
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 768})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    assert result.summary.preflight_short_circuited is True
    codes = [f.code for f in result.findings]
    assert codes == [FindingCode.EMBEDDING_DIMENSION_MISMATCH]
    assert result.findings[0].severity.value == "critical"
    assert result.ratios.lineage_coverage is None  # never even measured


def test_pii_policy_violation_when_denied_entity_type_present() -> None:
    """Acceptance scenario E: high-confidence SSN, policy denies it."""
    scenario = make_scenario(pii_findings=(("US_SSN", 0.95),))
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    policy = _bare_policy(pii=PiiPolicy(deny=["US_SSN"], max_confidence_allowed=0.0))
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope, policy=policy
    )
    pii_findings = [f for f in result.findings if f.code is FindingCode.PII_POLICY_VIOLATION]
    assert len(pii_findings) == 1
    assert pii_findings[0].evidence["entity_type"] == "US_SSN"
    # No raw SSN digits anywhere in the finding.
    assert "US_SSN" not in str(pii_findings[0].evidence.get("masked_preview"))


def test_pii_finding_without_policy_produces_no_violation() -> None:
    scenario = make_scenario(pii_findings=(("US_SSN", 0.95),))
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    assert all(f.code is not FindingCode.PII_POLICY_VIOLATION for f in result.findings)


def test_license_noassertion_reported_unknown_when_policy_says_fail() -> None:
    """Acceptance scenario E's other half: NOASSERTION license."""
    scenario = make_scenario(license_expression="NOASSERTION", license_method="repository_default")
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    policy = _bare_policy(licenses=LicensesPolicy(allow=["MIT"], unknown="fail"))
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope, policy=policy
    )
    codes = [f.code for f in result.findings]
    assert FindingCode.LICENSE_UNKNOWN in codes


def test_license_policy_violation_for_denied_spdx() -> None:
    scenario = make_scenario(license_expression="GPL-3.0-only", license_method="frontmatter")
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    policy = _bare_policy(licenses=LicensesPolicy(allow=["MIT", "Apache-2.0"]))
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope, policy=policy
    )
    codes = [f.code for f in result.findings]
    assert FindingCode.LICENSE_POLICY_VIOLATION in codes


def test_manifest_incomplete_finding_when_build_not_complete() -> None:
    scenario = make_scenario(build_status="incomplete")
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    codes = [f.code for f in result.findings]
    assert FindingCode.MANIFEST_INCOMPLETE in codes


def test_findings_are_stably_sorted_by_fingerprint() -> None:
    scenario = make_scenario()
    connector = list_connector([], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    fingerprints = [f.fingerprint for f in result.findings]
    assert fingerprints == sorted(fingerprints)


def test_summary_manifest_signed_reflects_signature_presence() -> None:
    unsigned = make_scenario(signed=False)
    signed = make_scenario(signed=True, uri="file:documents/signed.md")
    connector_unsigned = list_connector([unsigned.matching_point], vector_dimensions={"default": 4})
    connector_signed = list_connector([signed.matching_point], vector_dimensions={"default": 4})
    unsigned_result = reconcile_small_data(
        unsigned.manifest, connector_unsigned, target=unsigned.target, scope=unsigned.scope
    )
    signed_result = reconcile_small_data(
        signed.manifest, connector_signed, target=signed.target, scope=signed.scope
    )
    assert unsigned_result.summary.manifest_signed is False
    assert signed_result.summary.manifest_signed is True


def test_max_in_memory_points_guard_rail() -> None:
    scenario = make_scenario()
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    with pytest.raises(ValueError, match="max_in_memory_points"):
        reconcile_small_data(
            scenario.manifest,
            connector,
            target=scenario.target,
            scope=scenario.scope,
            max_in_memory_points=0,
        )
