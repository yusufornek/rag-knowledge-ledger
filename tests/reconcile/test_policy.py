"""Policy loading/evaluation tests, per the design specification sections 8.14 and 12.4."""

from __future__ import annotations

import pytest

from ragledger.reconcile.policy import (
    PolicyValidationError,
    evaluate_policy,
    load_policy_document,
)
from ragledger.reconcile.report import ConsistencyCaveat, Ratios, ReconciliationResult, Summary
from ragledger.reconcile.taxonomy import FindingCode, FindingSeverity, build_finding

_SPEC_EXAMPLE_POLICY = """
version: 1
name: production-knowledge-base
requirements:
  manifest_signature: required
  full_snapshot: required
  lineage_coverage_min: 0.99
findings:
  fail_on_severity: [critical, high]
pii:
  deny:
    - CREDIT_CARD
    - US_SSN
  max_confidence_allowed: 0.0
licenses:
  allow:
    - Apache-2.0
    - MIT
    - CC-BY-4.0
  unknown: fail
access:
  acl_required: true
  tenant_required: true
drift:
  stale_ratio_max: 0.0
  orphan_ratio_max: 0.0
"""


def _consistency(
    *, snapshot_kind: str = "full", completeness: str = "complete"
) -> ConsistencyCaveat:
    return ConsistencyCaveat(
        mode="strict_consistent",
        completeness=completeness,
        start_count=100,
        end_count=100,
        observed_count=100,
        degraded_confidence=completeness != "complete",
        snapshot_kind=snapshot_kind,
    )


def _result(
    *,
    findings=(),
    manifest_signed=True,
    snapshot_kind="full",
    completeness="complete",
    lineage_coverage=1.0,
    stale_ratio=0.0,
    orphan_ratio=0.0,
    acl_compliance=1.0,
) -> ReconciliationResult:
    findings = list(findings)
    return ReconciliationResult(
        summary=Summary(
            target="tgt",
            scope="scope-a",
            expected_bindings=100,
            observed_points=100,
            matched_points=100,
            finding_count=len(findings),
            manifest_signed=manifest_signed,
        ),
        ratios=Ratios(
            lineage_coverage=lineage_coverage,
            missing_ratio=0.0,
            orphan_ratio=orphan_ratio,
            stale_ratio=stale_ratio,
            acl_compliance=acl_compliance,
        ),
        findings=findings,
        consistency=_consistency(snapshot_kind=snapshot_kind, completeness=completeness),
    )


def test_spec_example_policy_loads_and_validates() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    assert document.name == "production-knowledge-base"
    assert document.requirements.manifest_signature == "required"
    assert document.pii is not None and "US_SSN" in document.pii.deny
    assert document.licenses is not None and document.licenses.unknown == "fail"


def test_unknown_top_level_key_is_a_hard_error() -> None:
    with pytest.raises(PolicyValidationError):
        load_policy_document(
            """
version: 1
name: bad-policy
requirements: {}
findings:
  fail_on_severity: [critical]
totally_unknown_key: true
"""
        )


def test_unknown_nested_key_is_a_hard_error() -> None:
    with pytest.raises(PolicyValidationError):
        load_policy_document(
            """
version: 1
name: bad-policy
requirements: {}
findings:
  fail_on_severity: [critical]
pii:
  deny: [US_SSN]
  bogus_field: 1
"""
        )


def test_policy_document_must_be_a_mapping() -> None:
    with pytest.raises(PolicyValidationError):
        load_policy_document("- just\n- a\n- list\n")


def test_evaluate_policy_pass_when_everything_clean() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    result = _result()
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "PASS"
    assert all(rule.verdict == "PASS" for rule in verdict.rule_results)


def test_evaluate_policy_fails_on_unsigned_manifest_when_required() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    result = _result(manifest_signed=False)
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "FAIL"
    failing = [r for r in verdict.rule_results if r.rule == "requirements.manifest_signature"]
    assert failing and failing[0].verdict == "FAIL"


def test_evaluate_policy_fails_on_sample_snapshot_when_full_required() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    result = _result(snapshot_kind="sample")
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "FAIL"


def test_evaluate_policy_fails_on_low_lineage_coverage() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    result = _result(lineage_coverage=0.5)
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "FAIL"


def test_evaluate_policy_inconclusive_when_lineage_coverage_not_applicable() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    result = _result(lineage_coverage=None)
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "INCONCLUSIVE"


def test_evaluate_policy_fails_on_critical_finding_severity() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    finding = build_finding(
        code=FindingCode.ACL_BROADER_THAN_SOURCE,
        target="tgt",
        scope="scope-a",
        subject_id="idx_1",
        affected_field="acl",
        severity=FindingSeverity.CRITICAL,
    )
    result = _result(findings=[finding])
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "FAIL"


def test_evaluate_policy_warns_without_failing_on_warn_only_severity() -> None:
    document = load_policy_document(
        """
version: 1
name: warn-only
requirements: {}
findings:
  fail_on_severity: [critical]
  warn_on_severity: [medium]
"""
    )
    finding = build_finding(
        code=FindingCode.PAYLOAD_DRIFT,
        target="tgt",
        scope="scope-a",
        subject_id="idx_1",
        affected_field="payload_hash",
    )
    assert finding.severity is FindingSeverity.MEDIUM
    result = _result(findings=[finding])
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "WARN"


def test_evaluate_policy_drift_ratio_over_max_fails() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    result = _result(stale_ratio=0.1)
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "FAIL"
    assert any(
        r.rule == "drift.stale_ratio_max" and r.verdict == "FAIL" for r in verdict.rule_results
    )


def test_evaluate_policy_drift_not_applicable_is_skipped_not_inconclusive() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    result = _result(stale_ratio=None)
    verdict = evaluate_policy(document, result)
    # A None drift ratio does not appear as a rule result at all.
    assert not any(r.rule == "drift.stale_ratio_max" for r in verdict.rule_results)


def test_evaluate_policy_license_unknown_fail_mode() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    finding = build_finding(
        code=FindingCode.LICENSE_UNKNOWN,
        target="tgt",
        scope="scope-a",
        subject_id="idx_1",
        affected_field="license",
    )
    result = _result(findings=[finding])
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "FAIL"
    assert any(r.rule == "licenses.unknown" and r.verdict == "FAIL" for r in verdict.rule_results)


def test_generic_count_rule_fails_over_threshold() -> None:
    document = load_policy_document(
        """
version: 1
name: generic-rule-policy
requirements: {}
findings:
  fail_on_severity: [critical]
rules:
  - category: count
    taxonomy_codes: [ORPHAN_IN_INDEX]
    comparator: gt
    threshold: 2
    verdict_on_violation: FAIL
"""
    )
    findings = [
        build_finding(
            code=FindingCode.ORPHAN_IN_INDEX,
            target="tgt",
            scope="s",
            subject_id=f"p{i}",
            affected_field="point_id",
        )
        for i in range(3)
    ]
    result = _result(findings=findings)
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "FAIL"


def test_generic_count_rule_passes_under_threshold() -> None:
    document = load_policy_document(
        """
version: 1
name: generic-rule-policy
requirements: {}
findings:
  fail_on_severity: [critical]
rules:
  - category: count
    taxonomy_codes: [ORPHAN_IN_INDEX]
    comparator: gt
    threshold: 5
    verdict_on_violation: FAIL
"""
    )
    findings = [
        build_finding(
            code=FindingCode.ORPHAN_IN_INDEX,
            target="tgt",
            scope="s",
            subject_id="p0",
            affected_field="point_id",
        )
    ]
    result = _result(findings=findings)
    verdict = evaluate_policy(document, result)
    assert verdict.verdict == "PASS"


def test_policy_verdict_default_principal_masking_is_hash() -> None:
    document = load_policy_document(_SPEC_EXAMPLE_POLICY)
    verdict = evaluate_policy(document, _result())
    assert verdict.principal_masking == "hash"
