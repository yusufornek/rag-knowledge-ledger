"""Tests for `ragledger.reports.snapshot_report`."""

from __future__ import annotations

from pathlib import Path

from ragledger.core.canonical import canonical_bytes
from ragledger.reports.snapshot_report import build_snapshot_report, render_snapshot_report_html


def test_build_report_matches_qdrant_fixture(snapshots_dir: Path) -> None:
    report = build_snapshot_report(snapshots_dir / "qdrant_support_kb.ndjson.zst")
    assert report["report_type"] == "ragledger.snapshot_report.v1"
    assert report["target_id"] == "support_kb"
    assert report["target_type"] == "qdrant"
    assert report["point_count"] == 3
    assert report["observed_point_count"] == 3
    assert report["consistency_completeness"] == "complete"
    assert set(report["vector_dimensions"]) == set(report["vector_names"])


def test_build_report_matches_pgvector_fixture_with_composite_ids(snapshots_dir: Path) -> None:
    report = build_snapshot_report(snapshots_dir / "pgvector_document_chunks.ndjson.zst")
    assert report["target_type"] == "pgvector"
    assert report["point_count"] == 3
    assert len(report["sample_locators"]) == 3


def test_report_json_dict_is_canonical_bytes_serializable(snapshots_dir: Path) -> None:
    report = build_snapshot_report(snapshots_dir / "qdrant_support_kb.ndjson.zst")
    encoded = canonical_bytes(report)
    assert encoded.startswith(b"{")


def test_report_sample_locators_bounded_and_never_the_full_point_list(
    snapshots_dir: Path,
) -> None:
    report = build_snapshot_report(snapshots_dir / "qdrant_support_kb.ndjson.zst")
    assert len(report["sample_locators"]) <= 10
    assert "point_id" not in report  # no raw point-id list ever surfaces in the report


def test_render_html_is_self_contained_and_plain(snapshots_dir: Path) -> None:
    html = render_snapshot_report_html(snapshots_dir / "qdrant_support_kb.ndjson.zst")
    assert html.startswith("<!doctype html>")
    assert "<script" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert "support_kb" in html
    assert all(ord(ch) < 0x1F000 for ch in html)


def test_render_html_shows_tenant_and_vector_stats(snapshots_dir: Path) -> None:
    html = render_snapshot_report_html(snapshots_dir / "qdrant_support_kb.ndjson.zst")
    assert "acme" in html
    assert "dense" in html
