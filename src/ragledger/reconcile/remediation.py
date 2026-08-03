"""Remediation planning, per the design specification section 8.14 (FR-133..FR-135).

`build_remediation_plan` is a pure function of a finding list: it groups
findings by (code, target, scope) and maps each group to one concrete,
read-only `RemediationAction` naming a candidate list (binding ids or
normalized point-id keys) and, for destructive-in-effect actions, an
explicit caution string (FR-135: "destructive candidate için explicit
caution"). Nothing in this module ever calls a connector or otherwise
touches a target (FR-134): the strongest thing an action can do is appear in
a plan a human or a separate, explicitly-authorized process later decides to
act on.
"""

from __future__ import annotations

from collections.abc import Sequence

from ragledger.reconcile.matching import normalize_point_id
from ragledger.reconcile.report import RemediationAction, RemediationPlan
from ragledger.reconcile.taxonomy import Finding, FindingCode

__all__ = ["build_remediation_plan"]

_REINDEX_CODES = frozenset(
    {
        FindingCode.MISSING_IN_INDEX,
        FindingCode.STALE_SOURCE,
        FindingCode.STALE_PARSE,
        FindingCode.STALE_CHUNKING,
        FindingCode.EMBEDDING_MODEL_MISMATCH,
    }
)
_DELETE_CANDIDATE_CODES = frozenset(
    {FindingCode.ORPHAN_IN_INDEX, FindingCode.DUPLICATE_POINT_ID, FindingCode.DUPLICATE_CONTENT}
)
_UPDATE_PAYLOAD_CODES = frozenset(
    {
        FindingCode.PAYLOAD_DRIFT,
        FindingCode.ACL_MISMATCH,
        FindingCode.TENANT_MISMATCH,
        FindingCode.ACL_MISSING,
        FindingCode.TENANT_MISSING,
        FindingCode.ACL_BROADER_THAN_SOURCE,
    }
)
_FULL_REBUILD_CODES = frozenset(
    {
        FindingCode.EMBEDDING_DIMENSION_MISMATCH,
        FindingCode.TARGET_SCHEMA_DRIFT,
        FindingCode.MANIFEST_INCOMPLETE,
    }
)

_DELETE_CAUTION = (
    "Deletes are destructive and irreversible against the live target; verify "
    "each candidate before acting -- this plan only lists candidates, it never "
    "deletes anything itself."
)
_REBUILD_CAUTION = (
    "A schema/dimension mismatch or an incomplete build means point-level fixes "
    "cannot resolve this; a full rebuild from a compatible manifest is required."
)


def build_remediation_plan(findings: Sequence[Finding]) -> RemediationPlan:
    """Build one `RemediationAction` per (finding code, target, scope) group."""
    groups: dict[tuple[str, str, str], list[Finding]] = {}
    for finding in findings:
        key = (finding.code.value, finding.locator.target, finding.locator.scope)
        groups.setdefault(key, []).append(finding)

    actions: list[RemediationAction] = []
    for (code_value, target, scope), group in sorted(groups.items()):
        code = FindingCode(code_value)
        action_name, destructive, caution = _action_for(code)
        candidates = sorted(
            {candidate for finding in group if (candidate := _candidate_id(finding)) is not None}
        )
        actions.append(
            RemediationAction(
                action=action_name,
                finding_codes=[code_value],
                target=target,
                scope=scope,
                candidates=candidates,
                destructive=destructive,
                caution=caution,
                rationale=f"{len(group)} finding(s) of {code_value} in this target/scope.",
            )
        )
    return RemediationPlan(actions=actions)


def _action_for(code: FindingCode) -> tuple[str, bool, str | None]:
    if code in _REINDEX_CODES:
        return "reindex_source", False, None
    if code in _DELETE_CANDIDATE_CODES:
        return "delete_point_candidate", True, _DELETE_CAUTION
    if code in _UPDATE_PAYLOAD_CODES:
        return "update_payload_candidate", False, None
    if code in _FULL_REBUILD_CODES:
        return "full_rebuild_required", True, _REBUILD_CAUTION
    return "review_required", False, None


def _candidate_id(finding: Finding) -> str | None:
    if finding.locator.binding_id:
        return finding.locator.binding_id
    if finding.locator.point_id is not None:
        return normalize_point_id(finding.locator.point_id)
    return None
