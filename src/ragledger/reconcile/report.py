"""Reconciliation result/report models, per PROJECT_SPEC.md sections 14.4, 14.5,
37.3, and 8.14/8.15.

Layering note: this module is the shared "report shape" leaf the rest of
`ragledger.reconcile` builds on --
`ragledger.reconcile.engine` constructs `ReconciliationResult`,
`ragledger.reconcile.policy` constructs `PolicyVerdict`, and
`ragledger.reconcile.remediation` constructs `RemediationPlan`; all three
import their output types from here rather than each other, so there is no
import cycle between engine/policy/remediation.

Every model here is canonical-JSON-serializable
(`ragledger.core.canonical.canonical_bytes` via `to_json_bytes`) and carries
no wall-clock timestamp: a reconciliation report's content is a pure
function of its manifest/snapshot/policy inputs, matching this project's
determinism convention (see `ragledger.core.models`).
"""

from __future__ import annotations

from pydantic import Field

from ragledger.connectors.base import ConsistencyInfo, SnapshotCompleteness
from ragledger.core.canonical import canonical_bytes
from ragledger.core.models import RagledgerModel
from ragledger.reconcile.taxonomy import Finding

__all__ = [
    "EXIT_EXECUTION_ERROR",
    "EXIT_PASS",
    "EXIT_POLICY_FAIL",
    "ConsistencyCaveat",
    "PolicyVerdict",
    "Ratios",
    "ReconciliationReport",
    "ReconciliationResult",
    "RemediationAction",
    "RemediationPlan",
    "RuleResult",
    "Summary",
    "consistency_caveat_from_info",
    "exit_code_for",
    "render_ci_summary",
    "to_json_bytes",
]


class Ratios(RagledgerModel):
    """Section 14.4's five ratios. `None` means "not applicable" (a zero
    denominator), per section 14.4: "Zero denominator 'not applicable',
    100% değil" -- never silently coerced to 0.0 or 1.0.
    """

    lineage_coverage: float | None = None
    missing_ratio: float | None = None
    orphan_ratio: float | None = None
    stale_ratio: float | None = None
    acl_compliance: float | None = None


def ratio(numerator: int, denominator: int) -> float | None:
    """Section 14.4's "not applicable" zero-denominator rule, shared by
    every ratio computation in `ragledger.reconcile.engine`."""
    if denominator == 0:
        return None
    return numerator / denominator


class ConsistencyCaveat(RagledgerModel):
    """One target/scope pass's consistency outcome, propagated from
    `ConsistencyInfo` (section 13.3/13.4) into the report."""

    mode: str
    completeness: str
    start_count: int | None = None
    end_count: int | None = None
    observed_count: int
    degraded_confidence: bool
    snapshot_kind: str = "full"
    detail: str | None = None


def consistency_caveat_from_info(
    info: ConsistencyInfo, *, snapshot_kind: str = "full"
) -> ConsistencyCaveat:
    return ConsistencyCaveat(
        mode=info.mode.value,
        completeness=info.completeness.value,
        start_count=info.start_count,
        end_count=info.end_count,
        observed_count=info.observed_count,
        degraded_confidence=info.completeness == SnapshotCompleteness.INCOMPLETE,
        snapshot_kind=snapshot_kind,
        detail=info.detail,
    )


class Summary(RagledgerModel):
    target: str
    scope: str
    expected_bindings: int
    observed_points: int
    matched_points: int
    finding_count: int
    finding_count_by_severity: dict[str, int] = Field(default_factory=dict)
    manifest_signed: bool = False
    preflight_short_circuited: bool = False


class ReconciliationResult(RagledgerModel):
    """The reconciliation engine's output for one (target, scope) run,
    before policy evaluation or remediation planning.

    `findings` is stably ordered (by fingerprint; see
    `ragledger.reconcile.engine`), so `findings_page` is a safe,
    deterministic pagination primitive for a later reporting/CLI layer
    (section 37.5's page-size limits) without this module needing to know
    anything about HTTP or a CLI.
    """

    summary: Summary
    ratios: Ratios
    findings: list[Finding] = Field(default_factory=list)
    consistency: ConsistencyCaveat
    manifest_id: str | None = None
    manifest_status: str | None = None

    def findings_page(self, *, offset: int = 0, limit: int | None = None) -> list[Finding]:
        if limit is None:
            return self.findings[offset:]
        return self.findings[offset : offset + limit]


class RuleResult(RagledgerModel):
    rule: str
    passed: bool
    verdict: str
    detail: str


class PolicyVerdict(RagledgerModel):
    policy_name: str
    verdict: str
    rule_results: list[RuleResult] = Field(default_factory=list)
    principal_masking: str = "hash"
    """The report masking mode applied to ACL principals (acceptance
    scenario D: "raw principal public report policy ile hash"). Fixed to
    `"hash"` in this milestone -- see `docs/reviews/m6-status-notes.md` for
    why a configurable mode is a documented gap, not an oversight."""


class RemediationAction(RagledgerModel):
    """One concrete, read-only remediation suggestion for a group of
    findings sharing a code/target/scope (FR-133). Never executed by
    anything in this package (FR-134): `ragledger.reconcile.remediation`
    only ever builds this model, never calls a connector.
    """

    action: str
    finding_codes: list[str] = Field(default_factory=list)
    target: str
    scope: str
    candidates: list[str] = Field(default_factory=list)
    destructive: bool = False
    caution: str | None = None
    rationale: str = ""


class RemediationPlan(RagledgerModel):
    actions: list[RemediationAction] = Field(default_factory=list)

    def to_csv_rows(self) -> list[list[str]]:
        """A CSV-ready row list (FR-135), header first."""
        rows: list[list[str]] = [
            ["action", "target", "scope", "destructive", "candidate_count", "rationale"]
        ]
        for entry in self.actions:
            rows.append(
                [
                    entry.action,
                    entry.target,
                    entry.scope,
                    str(entry.destructive),
                    str(len(entry.candidates)),
                    entry.rationale,
                ]
            )
        return rows


class ReconciliationReport(RagledgerModel):
    """The final bundle: a reconciliation result, its policy verdict, and
    its remediation plan (this milestone's deliverable #6)."""

    result: ReconciliationResult
    policy: PolicyVerdict
    remediation: RemediationPlan


def to_json_bytes(report: ReconciliationReport) -> bytes:
    """RFC 8785 canonical JSON bytes for `report` -- deterministic for the
    same inputs, no wall clock, matching this project's canonicalization
    convention (`ragledger.core.canonical`).
    """
    return canonical_bytes(report.model_dump(mode="json", exclude_none=True, by_alias=True))


_SEVERITY_DISPLAY_ORDER = ("critical", "high", "medium", "low")


def render_ci_summary(report: ReconciliationReport) -> str:
    """A compact, deterministic plain-text CI summary (no ANSI, no emoji)."""
    summary = report.result.summary
    ratios = report.result.ratios
    lines = [
        f"reconciliation: target={summary.target} scope={summary.scope} "
        f"verdict={report.policy.verdict}",
        f"points: expected={summary.expected_bindings} observed={summary.observed_points} "
        f"matched={summary.matched_points}",
        "findings: "
        + (
            " ".join(
                f"{severity}={summary.finding_count_by_severity.get(severity, 0)}"
                for severity in _SEVERITY_DISPLAY_ORDER
                if severity in summary.finding_count_by_severity
            )
            or "none"
        ),
        "ratios: "
        + " ".join(
            f"{name}={value:.4f}" if value is not None else f"{name}=not_applicable"
            for name, value in (
                ("lineage_coverage", ratios.lineage_coverage),
                ("missing_ratio", ratios.missing_ratio),
                ("orphan_ratio", ratios.orphan_ratio),
                ("stale_ratio", ratios.stale_ratio),
                ("acl_compliance", ratios.acl_compliance),
            )
        ),
    ]
    if report.result.consistency.degraded_confidence:
        lines.append(
            "consistency: degraded_confidence=true completeness="
            f"{report.result.consistency.completeness} "
            f"snapshot_kind={report.result.consistency.snapshot_kind}"
        )
    for rule_result in report.policy.rule_results:
        if rule_result.verdict != "PASS":
            lines.append(
                f"policy rule [{rule_result.verdict}] {rule_result.rule}: {rule_result.detail}"
            )
    for action in report.remediation.actions:
        lines.append(
            f"remediation: {action.action} target={action.target} scope={action.scope} "
            f"candidates={len(action.candidates)} destructive={action.destructive}"
        )
    lines.append(f"exit_code={exit_code_for(report)}")
    return "\n".join(lines) + "\n"


EXIT_PASS = 0
EXIT_POLICY_FAIL = 1
EXIT_EXECUTION_ERROR = 2


def exit_code_for(report: ReconciliationReport) -> int:
    """Machine-readable exit-code recommendation for later CLI wiring.

    `0` for PASS or WARN (a warning never blocks a CI gate on its own),
    `1` when the policy verdict is FAIL, `2` when it is INCONCLUSIVE (an
    inconclusive verdict means reconciliation could not determine
    compliance at all -- an execution-class problem, not a policy
    decision -- matching `RECONCILIATION_INCONCLUSIVE` in section 37.6's
    error-code list).
    """
    if report.policy.verdict == "INCONCLUSIVE":
        return EXIT_EXECUTION_ERROR
    if report.policy.verdict == "FAIL":
        return EXIT_POLICY_FAIL
    return EXIT_PASS
