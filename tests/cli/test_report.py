"""Tests for `ragledger report manifest|snapshot`.

Includes a PII-leak canary test (mirroring
`tests/governance/test_pii_leak_canary.py`'s approach): builds a
manifest over the synthetic corpus, which contains several unique,
deliberately recognizable canary PII values, generates both report
formats, and asserts none of the raw values ever appear in either.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from ragledger.cli import app

# Every raw canary value that appears, in full, in tests/fixtures/corpus/*
# (see tests/governance/test_pii_leak_canary.py for the source of truth).
_CANARY_VALUES = [
    "canary.leaktest@example.com",
    "555-010-1199",
]


def _build(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> Path:
    config = write_minimal_config(tmp_path / "ragledger.yml", root=corpus_dir)
    output = tmp_path / "manifest.json"
    result = runner.invoke(
        app,
        [
            "build",
            str(corpus_dir),
            "--config",
            str(config),
            "--output",
            str(output),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--cache",
            str(tmp_path / "cache"),
            "--epoch",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    return output


def test_report_manifest_json_default_goes_to_stdout(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    result = runner.invoke(app, ["report", "manifest", str(manifest)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["report_type"] == "ragledger.manifest_report.v1"
    assert report["statistics"]["chunk_count"] > 0


def test_report_manifest_json_matches_manifest_statistics(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest_path = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    manifest = json.loads(manifest_path.read_bytes())
    result = runner.invoke(app, ["report", "manifest", str(manifest_path)])
    report = json.loads(result.stdout)
    assert report["statistics"] == manifest["statistics"]
    assert report["integrity"]["manifest_hash"] == manifest["integrity"]["manifest_hash"]


def test_report_manifest_html_is_self_contained(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    output = tmp_path / "report.html"
    result = runner.invoke(
        app, ["report", "manifest", str(manifest), "--format", "html", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    html = output.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "<script" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_report_manifest_invalid_format_is_a_config_error(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    result = runner.invoke(app, ["report", "manifest", str(manifest), "--format", "yaml"])
    assert result.exit_code == 1
    assert "--format must be one of" in result.output


def test_report_manifest_missing_file_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["report", "manifest", str(tmp_path / "missing.json")])
    assert result.exit_code == 1


def test_report_snapshot_json_and_html(
    runner: CliRunner, snapshots_dir: Path, tmp_path: Path
) -> None:
    snapshot_path = snapshots_dir / "qdrant_support_kb.ndjson.zst"

    json_result = runner.invoke(app, ["report", "snapshot", str(snapshot_path)])
    assert json_result.exit_code == 0, json_result.output
    report = json.loads(json_result.stdout)
    assert report["report_type"] == "ragledger.snapshot_report.v1"
    assert report["point_count"] == 3

    html_output = tmp_path / "snapshot-report.html"
    html_result = runner.invoke(
        app,
        [
            "report",
            "snapshot",
            str(snapshot_path),
            "--format",
            "html",
            "--output",
            str(html_output),
        ],
    )
    assert html_result.exit_code == 0, html_result.output
    html = html_output.read_text(encoding="utf-8")
    assert "<script" not in html
    assert "support_kb" in html


def test_report_snapshot_missing_file_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["report", "snapshot", str(tmp_path / "missing.ndjson.zst")])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_report_no_canary_pii_value_leaks_into_json_or_html(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)

    json_result = runner.invoke(app, ["report", "manifest", str(manifest)])
    html_result = runner.invoke(app, ["report", "manifest", str(manifest), "--format", "html"])
    assert json_result.exit_code == 0
    assert html_result.exit_code == 0

    for canary in _CANARY_VALUES:
        assert canary not in json_result.stdout, f"canary leaked into JSON report: {canary!r}"
        assert canary not in html_result.stdout, f"canary leaked into HTML report: {canary!r}"

    # Sanity check the canary is actually live: the PII section should show
    # at least one EMAIL_ADDRESS finding, so an absent-value assertion above
    # is not vacuously true because detection silently regressed.
    report = json.loads(json_result.stdout)
    assert report["governance"]["pii"]["findings_by_entity_type"].get("EMAIL_ADDRESS", 0) >= 1
