"""Render a reconciliation report as a self-contained HTML page.

Mirrors `ragledger.reports.manifest_report`/`snapshot_report`: no separate
fact-collection pass, no network call, no `<script>` tag. This renderer
serializes exactly what `ragledger.reconcile.report.ReconciliationReport`
already carries -- summary, ratios, consistency caveat, policy verdict,
findings, and the remediation plan -- as HTML.

Every `Finding.evidence` dict reaching this module is already masked by
`ragledger.reconcile.taxonomy`/`ragledger.reconcile.engine` (ACL principals
hashed, PII reduced to `masked_preview`) before it is ever attached to a
`Finding`; this renderer performs no masking of its own, it only serializes
what it is given -- see `tests/reconcile/test_pii_masking_canary.py` and
`tests/cli/test_reconcile.py` for the canary tests that guard this.
"""

from __future__ import annotations

import json

from ragledger.reconcile.report import ReconciliationReport
from ragledger.reports._html import escape, page, stat_grid, table

_STATUS_CLASS = {
    "PASS": "status-ok",
    "WARN": "status-warn",
    "FAIL": "status-fail",
    "INCONCLUSIVE": "status-fail",
}

_RATIO_LABELS = (
    ("lineage_coverage", "Lineage coverage"),
    ("missing_ratio", "Missing ratio"),
    ("orphan_ratio", "Orphan ratio"),
    ("stale_ratio", "Stale ratio"),
    ("acl_compliance", "ACL compliance"),
)


def _format_ratio(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "not applicable"


def _finding_subject(locator_binding_id: str | None, locator_point_id: object) -> str:
    if locator_binding_id:
        return locator_binding_id
    if locator_point_id is not None:
        return str(locator_point_id)
    return "-"


def render_reconciliation_report_html(report: ReconciliationReport) -> str:
    """Render ``report`` as a self-contained HTML page."""
    summary = report.result.summary
    ratios = report.result.ratios
    consistency = report.result.consistency
    verdict = report.policy.verdict
    status_class = _STATUS_CLASS.get(verdict, "status-warn")

    subtitle = (
        f"target {summary.target} | scope {summary.scope} | "
        f"policy {report.policy.policy_name} | verdict {verdict}"
    )

    summary_stats = stat_grid(
        [
            ("Expected bindings", summary.expected_bindings),
            ("Observed points", summary.observed_points),
            ("Matched points", summary.matched_points),
            ("Findings", summary.finding_count),
            ("Manifest signed", "yes" if summary.manifest_signed else "no"),
        ]
    )
    severity_rows = sorted(summary.finding_count_by_severity.items())

    ratio_rows = [(label, _format_ratio(getattr(ratios, name))) for name, label in _RATIO_LABELS]

    consistency_rows = [
        ("Mode", consistency.mode),
        ("Completeness", consistency.completeness),
        ("Snapshot kind", consistency.snapshot_kind),
        ("Degraded confidence", "yes" if consistency.degraded_confidence else "no"),
        ("Detail", consistency.detail or "-"),
    ]

    rule_rows = [(rule.rule, rule.verdict, rule.detail) for rule in report.policy.rule_results]

    finding_rows = [
        (
            finding.code.value,
            finding.severity.value,
            _finding_subject(finding.locator.binding_id, finding.locator.point_id),
            finding.detail or "",
            json.dumps(finding.evidence, sort_keys=True, default=str),
        )
        for finding in report.result.findings
    ]

    remediation_rows = [
        (
            action.action,
            action.target,
            action.scope,
            len(action.candidates),
            "yes" if action.destructive else "no",
            action.rationale,
        )
        for action in report.remediation.actions
    ]

    sections = [
        f"<h2>Summary</h2>{summary_stats}"
        f'<p>Policy verdict: <span class="{status_class}">{escape(verdict)}</span></p>',
        "<h2>Findings by severity</h2>" + table(["Severity", "Count"], severity_rows),
        "<h2>Ratios</h2>" + table(["Ratio", "Value"], ratio_rows),
        "<h2>Consistency</h2>" + table(["Field", "Value"], consistency_rows),
        "<h2>Policy rules</h2>" + table(["Rule", "Verdict", "Detail"], rule_rows),
        "<h2>Findings</h2>"
        + table(["Code", "Severity", "Subject", "Detail", "Evidence"], finding_rows),
        "<h2>Remediation plan</h2>"
        + table(
            ["Action", "Target", "Scope", "Candidates", "Destructive", "Rationale"],
            remediation_rows,
        ),
    ]
    return page(
        f"ragledger reconciliation report: {summary.target}/{summary.scope}",
        subtitle,
        "".join(sections),
    )
