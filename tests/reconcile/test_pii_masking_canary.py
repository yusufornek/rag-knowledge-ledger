"""PII/principal masking canary: raw values must never appear in a
serialized reconciliation report, per the HARD RULES this milestone was
scoped under and PROJECT_SPEC.md acceptance scenarios D and E.

Uses `ragledger.governance.pii`'s real scanner (not a synthetic stand-in),
so the masked evidence exercised here is the actual masking convention the
rest of the codebase already relies on, not a mock of it.
"""

from __future__ import annotations

from typing import Any

from tests.reconcile.builders import FIXED_TIME, Scenario, list_connector, make_scenario

from ragledger.governance.pii import PiiScanConfig, build_pii_scan_assertion
from ragledger.reconcile.engine import reconcile_small_data
from ragledger.reconcile.policy import (
    FindingsPolicy,
    PiiPolicy,
    PolicyDocument,
    Requirements,
    evaluate_policy,
)
from ragledger.reconcile.remediation import build_remediation_plan
from ragledger.reconcile.report import ReconciliationReport, render_ci_summary, to_json_bytes
from ragledger.reconcile.taxonomy import FindingCode

RAW_SSN = "123-45-6787"
RAW_EMAIL = "jane.doe@example.com"
RAW_TEXT = f"Please update SSN {RAW_SSN} on file for {RAW_EMAIL}."

_CANARY_POLICY = PolicyDocument(
    name="canary-policy",
    requirements=Requirements(),
    findings=FindingsPolicy(fail_on_severity=["critical", "high"]),
    pii=PiiPolicy(deny=["US_SSN"], max_confidence_allowed=0.0),
)


def _scenario_with_real_pii_scan(acl_entries: tuple[str, ...]) -> tuple[Scenario, Any]:
    scenario = make_scenario(acl_entries=acl_entries)
    real_assertion = build_pii_scan_assertion(
        scenario.chunk.id, RAW_TEXT, PiiScanConfig(), FIXED_TIME
    )
    assert real_assertion.findings, "sanity check: the real scanner must find the synthetic SSN"
    updated_chunk = scenario.chunk.model_copy(update={"pii_assertion_ids": [real_assertion.id]})
    updated_manifest = scenario.manifest.model_copy(
        update={
            "chunks": [updated_chunk],
            "assertions": [*scenario.manifest.assertions, real_assertion],
        }
    )
    return scenario, updated_manifest


def _reconcile_with_acl_leak(scenario: Scenario, manifest: Any) -> ReconciliationReport:
    leaked_point = scenario.matching_point.model_copy(update={"acl": ["PUBLIC"]})
    connector = list_connector([leaked_point], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        manifest, connector, target=scenario.target, scope=scenario.scope, policy=_CANARY_POLICY
    )
    codes = {f.code for f in result.findings}
    assert FindingCode.PII_POLICY_VIOLATION in codes
    assert FindingCode.ACL_BROADER_THAN_SOURCE in codes
    verdict = evaluate_policy(_CANARY_POLICY, result)
    return ReconciliationReport(
        result=result, policy=verdict, remediation=build_remediation_plan(result.findings)
    )


def test_raw_ssn_never_appears_in_canonical_json_report() -> None:
    scenario, manifest = _scenario_with_real_pii_scan(acl_entries=("GROUP:finance",))
    report_bytes = to_json_bytes(_reconcile_with_acl_leak(scenario, manifest))
    assert RAW_SSN.encode() not in report_bytes


def test_raw_acl_principal_never_appears_in_canonical_json_report() -> None:
    scenario, manifest = _scenario_with_real_pii_scan(acl_entries=("USER:" + RAW_EMAIL,))
    report_bytes = to_json_bytes(_reconcile_with_acl_leak(scenario, manifest))
    assert RAW_EMAIL.encode() not in report_bytes


def test_raw_values_never_appear_in_ci_text_summary() -> None:
    scenario, manifest = _scenario_with_real_pii_scan(acl_entries=("USER:" + RAW_EMAIL,))
    text = render_ci_summary(_reconcile_with_acl_leak(scenario, manifest))
    assert RAW_SSN not in text
    assert RAW_EMAIL not in text


def test_masked_preview_still_partially_obscures_the_ssn() -> None:
    """The masked preview is a bounded partial disclosure (per
    `ragledger.governance.pii`'s own convention), not a full raw value: the
    leading digits are always hidden."""
    scenario, manifest = _scenario_with_real_pii_scan(acl_entries=("GROUP:finance",))
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    result = reconcile_small_data(
        manifest, connector, target=scenario.target, scope=scenario.scope, policy=_CANARY_POLICY
    )
    pii_findings = [f for f in result.findings if f.code is FindingCode.PII_POLICY_VIOLATION]
    assert len(pii_findings) == 1
    masked_preview = pii_findings[0].evidence["masked_preview"]
    assert masked_preview != RAW_SSN
    assert not masked_preview.startswith(RAW_SSN[:7])
