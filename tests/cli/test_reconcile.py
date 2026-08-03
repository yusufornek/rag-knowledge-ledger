"""Tests for `ragledger reconcile`, per the design specification section 17.1.

Builds synthetic manifest + `.ndjson.zst` snapshot fixtures with
`tests.reconcile.builders` (the same builders `tests/reconcile/*` uses for
the engine itself), then drives the CLI end to end through
`typer.testing.CliRunner` -- no live target, no network.

Includes a masked-evidence canary (mirroring
`tests/reconcile/test_pii_masking_canary.py` and
`tests/cli/test_report.py`'s PII-leak canary): a raw SSN and a raw ACL
principal identifier must never appear in the reconcile command's JSON
report, HTML report, or CI-summary stdout, even though both directly
caused the findings the policy failed on.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ragledger.cli import app
from ragledger.core.manifest import write_manifest
from ragledger.governance.pii import PiiScanConfig, build_pii_scan_assertion
from tests.reconcile.builders import (
    FIXED_TIME,
    SCOPE,
    TARGET,
    make_bulk_dataset,
    make_scenario,
    write_ndjson_snapshot,
)

_PASS_POLICY = """\
version: 1
name: pass-policy
requirements: {}
findings:
  fail_on_severity: [critical]
"""

_FAIL_POLICY = """\
version: 1
name: fail-policy
requirements: {}
findings:
  fail_on_severity: [critical, high]
"""

_PII_DENY_POLICY = """\
version: 1
name: pii-deny-policy
requirements: {}
findings:
  fail_on_severity: [critical, high]
pii:
  deny: [US_SSN]
  max_confidence_allowed: 0.0
"""

RAW_SSN = "123-45-6787"
RAW_PRINCIPAL_EMAIL = "jane.doe@example.com"
RAW_TEXT = f"Please update SSN {RAW_SSN} on file for {RAW_PRINCIPAL_EMAIL}."


def _write_clean_fixture(tmp_path: Path) -> tuple[Path, Path]:
    scenario = make_scenario()
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, scenario.manifest)
    snapshot_path = tmp_path / "snapshot.ndjson.zst"
    write_ndjson_snapshot(
        snapshot_path, [scenario.matching_point], target=scenario.target, scope=scenario.scope
    )
    return manifest_path, snapshot_path


def _write_missing_point_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A manifest with one expected binding and an EMPTY observed snapshot:
    always produces exactly one `MISSING_IN_INDEX` (default severity HIGH)
    finding, the fixture the fail-policy tests gate on."""
    scenario = make_scenario()
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, scenario.manifest)
    snapshot_path = tmp_path / "snapshot.ndjson.zst"
    write_ndjson_snapshot(snapshot_path, [], target=scenario.target, scope=scenario.scope)
    return manifest_path, snapshot_path


def test_reconcile_clean_pass_with_no_policy_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    manifest_path, snapshot_path = _write_clean_fixture(tmp_path)
    result = runner.invoke(app, ["reconcile", str(manifest_path), str(snapshot_path)])
    assert result.exit_code == 0, result.output
    assert "verdict=PASS" in result.output
    assert "exit_code=0" in result.output


def test_reconcile_policy_pass_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    manifest_path, snapshot_path = _write_clean_fixture(tmp_path)
    policy_path = tmp_path / "policy.yml"
    policy_path.write_text(_PASS_POLICY, encoding="utf-8")
    result = runner.invoke(
        app, ["reconcile", str(manifest_path), str(snapshot_path), "--policy", str(policy_path)]
    )
    assert result.exit_code == 0, result.output
    assert "verdict=PASS" in result.output


def test_reconcile_policy_fail_exits_one(runner: CliRunner, tmp_path: Path) -> None:
    manifest_path, snapshot_path = _write_missing_point_fixture(tmp_path)
    policy_path = tmp_path / "policy.yml"
    policy_path.write_text(_FAIL_POLICY, encoding="utf-8")
    result = runner.invoke(
        app, ["reconcile", str(manifest_path), str(snapshot_path), "--policy", str(policy_path)]
    )
    assert result.exit_code == 1, result.output
    assert "verdict=FAIL" in result.output
    assert "exit_code=1" in result.output


def test_reconcile_missing_manifest_file_is_an_execution_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "reconcile",
            str(tmp_path / "does-not-exist.json"),
            str(tmp_path / "also-missing.ndjson.zst"),
        ],
    )
    assert result.exit_code == 2, result.output


def test_reconcile_invalid_policy_document_is_an_execution_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    manifest_path, snapshot_path = _write_clean_fixture(tmp_path)
    policy_path = tmp_path / "bad-policy.yml"
    policy_path.write_text("version: 1\nname: broken\nunknown_top_level_key: true\n", "utf-8")
    result = runner.invoke(
        app, ["reconcile", str(manifest_path), str(snapshot_path), "--policy", str(policy_path)]
    )
    assert result.exit_code == 2, result.output
    assert "invalid policy document" in result.output


def test_reconcile_json_output_matches_ci_summary(runner: CliRunner, tmp_path: Path) -> None:
    manifest_path, snapshot_path = _write_clean_fixture(tmp_path)
    output_path = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "reconcile",
            str(manifest_path),
            str(snapshot_path),
            "--output",
            str(output_path),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output_path.read_bytes())
    assert report["policy"]["verdict"] == "PASS"
    assert report["result"]["summary"]["matched_points"] == 1
    assert report["result"]["summary"]["target"] == TARGET
    assert report["result"]["summary"]["scope"] == SCOPE
    assert report["remediation"]["actions"] == []


def test_reconcile_html_output_is_self_contained(runner: CliRunner, tmp_path: Path) -> None:
    manifest_path, snapshot_path = _write_missing_point_fixture(tmp_path)
    html_path = tmp_path / "report.html"
    result = runner.invoke(
        app, ["reconcile", str(manifest_path), str(snapshot_path), "--html", str(html_path)]
    )
    assert result.exit_code == 0, result.output
    html = html_path.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "<script" not in html
    assert "MISSING_IN_INDEX" in html


def test_reconcile_big_data_forced_path_matches_default_path(
    runner: CliRunner, tmp_path: Path
) -> None:
    manifest, points = make_bulk_dataset(matched_count=25, target=TARGET, scope=SCOPE)
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, manifest)
    snapshot_path = tmp_path / "snapshot.ndjson.zst"
    write_ndjson_snapshot(snapshot_path, points, target=TARGET, scope=SCOPE)
    work_dir = tmp_path / "work"

    result = runner.invoke(
        app,
        [
            "reconcile",
            str(manifest_path),
            str(snapshot_path),
            "--big-data",
            "--work-dir",
            str(work_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "matched=25" in result.output
    assert "verdict=PASS" in result.output


def test_reconcile_masked_evidence_canary_json_html_and_stdout(
    runner: CliRunner, tmp_path: Path
) -> None:
    scenario = make_scenario(acl_entries=("USER:" + RAW_PRINCIPAL_EMAIL,))
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
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, updated_manifest)

    leaked_point = scenario.matching_point.model_copy(update={"acl": ["PUBLIC"]})
    snapshot_path = tmp_path / "snapshot.ndjson.zst"
    write_ndjson_snapshot(
        snapshot_path, [leaked_point], target=scenario.target, scope=scenario.scope
    )

    policy_path = tmp_path / "policy.yml"
    policy_path.write_text(_PII_DENY_POLICY, encoding="utf-8")
    output_path = tmp_path / "report.json"
    html_path = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "reconcile",
            str(manifest_path),
            str(snapshot_path),
            "--policy",
            str(policy_path),
            "--output",
            str(output_path),
            "--html",
            str(html_path),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "verdict=FAIL" in result.output

    json_bytes = output_path.read_bytes()
    html_text = html_path.read_text(encoding="utf-8")
    for raw in (RAW_SSN, RAW_PRINCIPAL_EMAIL):
        assert raw.encode() not in json_bytes, f"canary leaked into JSON report: {raw!r}"
        assert raw not in html_text, f"canary leaked into HTML report: {raw!r}"
        assert raw not in result.output, f"canary leaked into CLI stdout/stderr: {raw!r}"

    # Sanity check the canary is actually live: the report should carry both
    # a PII policy violation and an ACL finding, so the negative assertions
    # above are not vacuously true because detection silently regressed.
    report = json.loads(json_bytes)
    codes = {finding["code"] for finding in report["result"]["findings"]}
    assert "PII_POLICY_VIOLATION" in codes
    assert "ACL_BROADER_THAN_SOURCE" in codes
