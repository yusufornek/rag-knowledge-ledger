"""Report model tests: ratio "not applicable" handling, canonical
serialization, CI text summary, exit codes, and pagination.
"""

from __future__ import annotations

from ragledger.reconcile.report import (
    EXIT_EXECUTION_ERROR,
    EXIT_PASS,
    EXIT_POLICY_FAIL,
    ConsistencyCaveat,
    PolicyVerdict,
    Ratios,
    ReconciliationReport,
    ReconciliationResult,
    RemediationPlan,
    RuleResult,
    Summary,
    exit_code_for,
    ratio,
    render_ci_summary,
    to_json_bytes,
)
from ragledger.reconcile.taxonomy import FindingCode, FindingSeverity, build_finding


def _consistency() -> ConsistencyCaveat:
    return ConsistencyCaveat(
        mode="strict_consistent",
        completeness="complete",
        start_count=10,
        end_count=10,
        observed_count=10,
        degraded_confidence=False,
    )


def _result(findings=()) -> ReconciliationResult:
    findings = list(findings)
    return ReconciliationResult(
        summary=Summary(
            target="tgt",
            scope="scope-a",
            expected_bindings=10,
            observed_points=10,
            matched_points=10 - len(findings),
            finding_count=len(findings),
            finding_count_by_severity={},
        ),
        ratios=Ratios(
            lineage_coverage=1.0,
            missing_ratio=0.0,
            orphan_ratio=0.0,
            stale_ratio=0.0,
            acl_compliance=1.0,
        ),
        findings=findings,
        consistency=_consistency(),
    )


def test_ratio_zero_denominator_is_not_applicable() -> None:
    assert ratio(0, 0) is None
    assert ratio(5, 0) is None
    assert ratio(0, 5) == 0.0
    assert ratio(5, 10) == 0.5


def test_findings_page_paginates_stably() -> None:
    findings = [
        build_finding(
            code=FindingCode.ORPHAN_IN_INDEX,
            target="tgt",
            scope="s",
            subject_id=f"p{i}",
            affected_field="point_id",
        )
        for i in range(5)
    ]
    result = _result(findings)
    assert len(result.findings_page(offset=0, limit=2)) == 2
    assert len(result.findings_page(offset=4, limit=2)) == 1
    assert result.findings_page() == findings


def test_to_json_bytes_is_deterministic() -> None:
    result = _result()
    report = ReconciliationReport(
        result=result,
        policy=PolicyVerdict(policy_name="p", verdict="PASS", rule_results=[]),
        remediation=RemediationPlan(),
    )
    first = to_json_bytes(report)
    second = to_json_bytes(report)
    assert first == second
    # Rebuilding an equal report from scratch produces byte-identical output.
    rebuilt = ReconciliationReport(
        result=_result(),
        policy=PolicyVerdict(policy_name="p", verdict="PASS", rule_results=[]),
        remediation=RemediationPlan(),
    )
    assert to_json_bytes(rebuilt) == first


def test_to_json_bytes_has_no_trailing_newline_and_is_utf8() -> None:
    report = ReconciliationReport(
        result=_result(),
        policy=PolicyVerdict(policy_name="p", verdict="PASS"),
        remediation=RemediationPlan(),
    )
    data = to_json_bytes(report)
    assert not data.endswith(b"\n")
    data.decode("utf-8")  # must not raise


def test_exit_code_pass_and_warn_are_zero() -> None:
    for verdict in ("PASS", "WARN"):
        report = ReconciliationReport(
            result=_result(),
            policy=PolicyVerdict(policy_name="p", verdict=verdict),
            remediation=RemediationPlan(),
        )
        assert exit_code_for(report) == EXIT_PASS


def test_exit_code_fail_is_one() -> None:
    report = ReconciliationReport(
        result=_result(),
        policy=PolicyVerdict(policy_name="p", verdict="FAIL"),
        remediation=RemediationPlan(),
    )
    assert exit_code_for(report) == EXIT_POLICY_FAIL


def test_exit_code_inconclusive_is_two() -> None:
    report = ReconciliationReport(
        result=_result(),
        policy=PolicyVerdict(policy_name="p", verdict="INCONCLUSIVE"),
        remediation=RemediationPlan(),
    )
    assert exit_code_for(report) == EXIT_EXECUTION_ERROR


def test_render_ci_summary_is_plain_text_and_deterministic() -> None:
    findings = [
        build_finding(
            code=FindingCode.ACL_MISSING,
            target="tgt",
            scope="scope-a",
            subject_id="idx_1",
            affected_field="acl",
            severity=FindingSeverity.CRITICAL,
        )
    ]
    report = ReconciliationReport(
        result=_result(findings),
        policy=PolicyVerdict(
            policy_name="prod",
            verdict="FAIL",
            rule_results=[
                RuleResult(
                    rule="findings.fail_on_severity",
                    passed=False,
                    verdict="FAIL",
                    detail="1 critical finding",
                )
            ],
        ),
        remediation=RemediationPlan(),
    )
    text = render_ci_summary(report)
    assert "verdict=FAIL" in text
    assert "exit_code=1" in text
    assert "\x1b" not in text  # no ANSI escape codes
    assert render_ci_summary(report) == text  # deterministic
