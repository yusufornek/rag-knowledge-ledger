"""Remediation plan tests, per PROJECT_SPEC.md section 8.14 (FR-133..FR-135)."""

from __future__ import annotations

from ragledger.reconcile.matching import normalize_point_id
from ragledger.reconcile.remediation import build_remediation_plan
from ragledger.reconcile.taxonomy import FindingCode, build_finding


def test_missing_in_index_suggests_reindex_and_is_not_destructive() -> None:
    finding = build_finding(
        code=FindingCode.MISSING_IN_INDEX,
        target="tgt",
        scope="scope-a",
        subject_id="idx_1",
        affected_field="point_id",
        binding_id="idx_1",
    )
    plan = build_remediation_plan([finding])
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action == "reindex_source"
    assert action.destructive is False
    assert action.caution is None
    assert action.candidates == ["idx_1"]


def test_orphan_in_index_suggests_delete_candidate_and_is_destructive_with_caution() -> None:
    finding = build_finding(
        code=FindingCode.ORPHAN_IN_INDEX,
        target="tgt",
        scope="scope-a",
        subject_id="pt_1",
        affected_field="point_id",
        point_id="pt_1",
    )
    plan = build_remediation_plan([finding])
    action = plan.actions[0]
    assert action.action == "delete_point_candidate"
    assert action.destructive is True
    assert action.caution is not None
    assert "irreversible" in action.caution


def test_embedding_dimension_mismatch_requires_full_rebuild() -> None:
    finding = build_finding(
        code=FindingCode.EMBEDDING_DIMENSION_MISMATCH,
        target="tgt",
        scope="scope-a",
        subject_id="tgt:scope-a",
        affected_field="dimension",
    )
    plan = build_remediation_plan([finding])
    action = plan.actions[0]
    assert action.action == "full_rebuild_required"
    assert action.destructive is True
    assert action.caution is not None


def test_payload_drift_suggests_update_payload_not_destructive() -> None:
    finding = build_finding(
        code=FindingCode.PAYLOAD_DRIFT,
        target="tgt",
        scope="scope-a",
        subject_id="idx_1",
        affected_field="payload_hash",
        binding_id="idx_1",
    )
    plan = build_remediation_plan([finding])
    action = plan.actions[0]
    assert action.action == "update_payload_candidate"
    assert action.destructive is False


def test_findings_grouped_by_code_target_scope() -> None:
    findings = [
        build_finding(
            code=FindingCode.ORPHAN_IN_INDEX,
            target="tgt",
            scope="scope-a",
            subject_id=f"pt_{i}",
            affected_field="point_id",
            point_id=f"pt_{i}",
        )
        for i in range(3)
    ]
    plan = build_remediation_plan(findings)
    assert len(plan.actions) == 1
    expected_candidates = sorted(normalize_point_id(f"pt_{i}") for i in range(3))
    assert sorted(plan.actions[0].candidates) == expected_candidates


def test_different_targets_produce_separate_actions() -> None:
    findings = [
        build_finding(
            code=FindingCode.ORPHAN_IN_INDEX,
            target="tgt-a",
            scope="scope-a",
            subject_id="pt_1",
            affected_field="point_id",
            point_id="pt_1",
        ),
        build_finding(
            code=FindingCode.ORPHAN_IN_INDEX,
            target="tgt-b",
            scope="scope-a",
            subject_id="pt_1",
            affected_field="point_id",
            point_id="pt_1",
        ),
    ]
    plan = build_remediation_plan(findings)
    assert len(plan.actions) == 2
    assert {action.target for action in plan.actions} == {"tgt-a", "tgt-b"}


def test_plan_never_calls_any_target_it_is_pure_data() -> None:
    """Structural guarantee: `RemediationPlan`/`RemediationAction` are plain
    pydantic models with no method that could execute anything, and
    `build_remediation_plan` never imports a connector."""
    import ragledger.reconcile.remediation as remediation_module

    assert "VectorTargetConnector" not in dir(remediation_module)
    plan = build_remediation_plan([])
    assert plan.actions == []


def test_to_csv_rows_has_header_and_one_row_per_action() -> None:
    finding = build_finding(
        code=FindingCode.STALE_SOURCE,
        target="tgt",
        scope="scope-a",
        subject_id="idx_1",
        affected_field="source_version_id",
        binding_id="idx_1",
    )
    plan = build_remediation_plan([finding])
    rows = plan.to_csv_rows()
    assert rows[0] == ["action", "target", "scope", "destructive", "candidate_count", "rationale"]
    assert len(rows) == 2
    assert rows[1][0] == "reindex_source"
