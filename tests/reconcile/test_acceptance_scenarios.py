"""Acceptance scenarios A-H, per PROJECT_SPEC.md section 28.

Scenario H (scale) is covered by `test_engine_big_data.py`'s 100k-point,
bounded-runtime, cancel/restart tests rather than duplicated here.

Scenario F (signing) exercises only the reconciliation-facing half of that
scenario -- `PolicyDocument.requirements.manifest_signature` gating on
signature *presence* -- not manifest tamper/verify-integrity, which is
`ragledger.core.signing`'s already-tested responsibility, out of this
package's scope; see `docs/reviews/m6-status-notes.md`.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from tests.reconcile.builders import list_connector, make_scenario, write_ndjson_snapshot

from ragledger.reconcile.engine import reconcile_small_data
from ragledger.reconcile.policy import (
    FindingsPolicy,
    LicensesPolicy,
    PiiPolicy,
    PolicyDocument,
    Requirements,
    evaluate_policy,
)
from ragledger.reconcile.remediation import build_remediation_plan
from ragledger.reconcile.report import ReconciliationReport, to_json_bytes
from ragledger.reconcile.taxonomy import FindingCode


def _fail_critical_high_policy(**overrides: object) -> PolicyDocument:
    base: dict[str, object] = {
        "name": "acceptance-scenario-policy",
        "requirements": Requirements(),
        "findings": FindingsPolicy(fail_on_severity=["critical", "high"]),
    }
    base.update(overrides)
    return PolicyDocument(**base)


# --------------------------------------------------------------------------
# A: Stale policy
# --------------------------------------------------------------------------


def test_scenario_a_stale_source_fails_policy_with_reindex_remediation() -> None:
    scenario = make_scenario(uri="file:documents/refund.pdf", body=b"refund policy page 4 original")
    stale_point = scenario.matching_point.model_copy(
        update={"source_version_id": "ver_" + "old" * 20}
    )
    connector = list_connector([stale_point], vector_dimensions={"default": 4})

    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )
    stale_findings = [f for f in result.findings if f.code is FindingCode.STALE_SOURCE]
    assert len(stale_findings) == 1
    assert stale_findings[0].affected_lineage.source_id == scenario.source.id

    verdict = evaluate_policy(_fail_critical_high_policy(), result)
    assert verdict.verdict == "FAIL"

    plan = build_remediation_plan(result.findings)
    reindex_actions = [a for a in plan.actions if a.action == "reindex_source"]
    assert len(reindex_actions) == 1
    assert reindex_actions[0].destructive is False


# --------------------------------------------------------------------------
# B: Orphan deletion
# --------------------------------------------------------------------------


def test_scenario_b_deleted_source_leaves_orphan_with_tombstone_hint_no_auto_delete() -> None:
    scenario = make_scenario()
    # Source was deleted: the manifest carries no index binding, no source,
    # and no lineage record for it at all (as if a fresh build ran after
    # the source's removal), but the target still has the old point.
    empty_manifest = scenario.manifest.model_copy(
        update={
            "index_bindings": [],
            "sources": [],
            "parse_runs": [],
            "chunks": [],
            "embeddings": [],
        }
    )
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})

    result = reconcile_small_data(
        empty_manifest, connector, target=scenario.target, scope=scenario.scope
    )
    orphan_findings = [f for f in result.findings if f.code is FindingCode.ORPHAN_IN_INDEX]
    assert len(orphan_findings) == 1
    assert orphan_findings[0].evidence.get("tombstone_hint") is True

    plan = build_remediation_plan(result.findings)
    delete_actions = [a for a in plan.actions if a.action == "delete_point_candidate"]
    assert len(delete_actions) == 1
    assert delete_actions[0].destructive is True
    assert delete_actions[0].caution is not None
    # The plan is data only -- nothing here ever calls a connector method
    # that could mutate the target.


# --------------------------------------------------------------------------
# C: Embedding mismatch
# --------------------------------------------------------------------------


def test_scenario_c_dimension_mismatch_short_circuits_preflight() -> None:
    scenario = make_scenario(dimension=1024)
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 768})

    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope
    )

    assert result.summary.preflight_short_circuited is True
    assert [f.code for f in result.findings] == [FindingCode.EMBEDDING_DIMENSION_MISMATCH]
    assert result.findings[0].severity.value == "critical"


# --------------------------------------------------------------------------
# D: ACL leak
# --------------------------------------------------------------------------


def test_scenario_d_acl_leak_via_ndjson_connector_masks_raw_principal() -> None:
    scenario = make_scenario(acl_entries=("GROUP:finance",))
    leaked_point = scenario.matching_point.model_copy(update={"acl": ["PUBLIC"]})

    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "snapshot.ndjson.zst"
        connector = write_ndjson_snapshot(
            snapshot_path, [leaked_point], target=scenario.target, scope=scenario.scope
        )
        result = reconcile_small_data(
            scenario.manifest, connector, target=scenario.target, scope=scenario.scope
        )

    acl_findings = [f for f in result.findings if f.code is FindingCode.ACL_BROADER_THAN_SOURCE]
    assert len(acl_findings) == 1
    assert acl_findings[0].severity.value == "critical"

    verdict = evaluate_policy(_fail_critical_high_policy(), result)
    assert verdict.verdict == "FAIL"

    report = ReconciliationReport(
        result=result, policy=verdict, remediation=build_remediation_plan(result.findings)
    )
    report_bytes = to_json_bytes(report)
    assert b"finance" not in report_bytes


# --------------------------------------------------------------------------
# E: PII / license
# --------------------------------------------------------------------------


def test_scenario_e_pii_and_noassertion_license_fail_policy_without_raw_leak() -> None:
    scenario = make_scenario(
        pii_findings=(("US_SSN", 0.9),),
        license_expression="NOASSERTION",
        license_method="repository_default",
    )
    connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    policy = _fail_critical_high_policy(
        pii=PiiPolicy(deny=["US_SSN"], max_confidence_allowed=0.0),
        licenses=LicensesPolicy(allow=["MIT"], unknown="fail"),
    )

    result = reconcile_small_data(
        scenario.manifest, connector, target=scenario.target, scope=scenario.scope, policy=policy
    )
    codes = {f.code for f in result.findings}
    assert FindingCode.PII_POLICY_VIOLATION in codes
    assert FindingCode.LICENSE_UNKNOWN in codes

    verdict = evaluate_policy(policy, result)
    assert verdict.verdict == "FAIL"

    report = ReconciliationReport(
        result=result, policy=verdict, remediation=build_remediation_plan(result.findings)
    )
    report_bytes = to_json_bytes(report)
    # No raw SSN-shaped digit sequence anywhere in the serialized report.
    assert re.search(rb"\d{3}-\d{2}-\d{4}", report_bytes) is None


# --------------------------------------------------------------------------
# F: Signing (reconciliation-facing half only; see module docstring)
# --------------------------------------------------------------------------


def test_scenario_f_manifest_signature_requirement() -> None:
    signed = make_scenario(signed=True)
    unsigned = make_scenario(signed=False, uri="file:documents/unsigned.md")
    policy = _fail_critical_high_policy(requirements=Requirements(manifest_signature="required"))

    for scenario, expect_pass in ((signed, True), (unsigned, False)):
        connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
        result = reconcile_small_data(
            scenario.manifest, connector, target=scenario.target, scope=scenario.scope
        )
        verdict = evaluate_policy(policy, result)
        signature_rule = next(
            r for r in verdict.rule_results if r.rule == "requirements.manifest_signature"
        )
        assert signature_rule.passed is expect_pass


# --------------------------------------------------------------------------
# G: Connector parity
# --------------------------------------------------------------------------


def test_scenario_g_reconciliation_is_identical_regardless_of_connector_implementation() -> None:
    """The same logical points, reconciled once through a plain in-memory
    connector and once through an NDJSON-snapshot-replay connector, produce
    identical findings and ratios -- reconciliation only ever sees the
    vendor-neutral `NormalizedPoint` shape, never a connector-specific
    representation, so which connector produced the points cannot matter.
    """
    scenario = make_scenario()

    memory_connector = list_connector([scenario.matching_point], vector_dimensions={"default": 4})
    memory_result = reconcile_small_data(
        scenario.manifest, memory_connector, target=scenario.target, scope=scenario.scope
    )

    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "snapshot.ndjson.zst"
        ndjson_connector = write_ndjson_snapshot(
            snapshot_path, [scenario.matching_point], target=scenario.target, scope=scenario.scope
        )
        ndjson_result = reconcile_small_data(
            scenario.manifest, ndjson_connector, target=scenario.target, scope=scenario.scope
        )

    assert memory_result.findings == ndjson_result.findings
    assert memory_result.ratios == ndjson_result.ratios
    assert memory_result.summary.matched_points == ndjson_result.summary.matched_points == 1
