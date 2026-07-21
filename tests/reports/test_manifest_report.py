"""Tests for `ragledger.reports.manifest_report`."""

from __future__ import annotations

from ragledger.core.canonical import canonical_bytes
from ragledger.core.models import ManifestEnvelope
from ragledger.reports.manifest_report import build_manifest_report, render_manifest_report_html

_CANARY_VALUES = ["canary.leaktest@example.com", "555-010-1199"]


def test_report_structure_matches_manifest_statistics(built_manifest: ManifestEnvelope) -> None:
    report = build_manifest_report(built_manifest)
    assert report["report_type"] == "ragledger.manifest_report.v1"
    assert report["namespace"] == "reports-test"
    assert report["statistics"]["source_count"] == built_manifest.statistics.source_count
    assert report["statistics"]["chunk_count"] == built_manifest.statistics.chunk_count
    assert report["integrity"]["manifest_hash"] == built_manifest.integrity.manifest_hash


def test_report_source_media_type_counts_sum_to_source_count(
    built_manifest: ManifestEnvelope,
) -> None:
    report = build_manifest_report(built_manifest)
    total = sum(report["sources"]["by_media_type"].values())
    assert total == len(built_manifest.sources)


def test_report_pii_findings_are_present_and_masked_only(built_manifest: ManifestEnvelope) -> None:
    report = build_manifest_report(built_manifest)
    pii = report["governance"]["pii"]
    assert pii["findings_by_entity_type"].get("EMAIL_ADDRESS", 0) >= 1
    example = pii["masked_examples_by_entity_type"]["EMAIL_ADDRESS"]
    assert "canary.leaktest@example.com" not in example
    assert "*" in example


def test_report_license_and_acl_and_tenant_sections_are_populated(
    built_manifest: ManifestEnvelope,
) -> None:
    report = build_manifest_report(built_manifest)
    source_count = len(built_manifest.sources)
    assert sum(report["governance"]["license"]["effective_expression_counts"].values()) > 0
    assert report["governance"]["acl"]["entry_set_counts"] == {"PUBLIC": source_count}
    assert report["governance"]["tenant"]["value_counts"] == {"acme": source_count}


def test_report_json_dict_is_canonical_bytes_serializable(built_manifest: ManifestEnvelope) -> None:
    report = build_manifest_report(built_manifest)
    encoded = canonical_bytes(report)
    assert encoded.startswith(b"{")


def test_report_json_is_deterministic_across_calls(built_manifest: ManifestEnvelope) -> None:
    first = canonical_bytes(build_manifest_report(built_manifest))
    second = canonical_bytes(build_manifest_report(built_manifest))
    assert first == second


def test_render_html_is_self_contained_and_plain(built_manifest: ManifestEnvelope) -> None:
    html = render_manifest_report_html(built_manifest)
    assert html.startswith("<!doctype html>")
    assert "<script" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert "reports-test" in html
    # No emoji: every codepoint stays below the emoji/pictograph block start.
    assert all(ord(ch) < 0x1F000 for ch in html)


def test_render_html_escapes_a_hostile_namespace(built_manifest: ManifestEnvelope) -> None:
    hostile = built_manifest.model_copy(update={"namespace": "<script>alert(1)</script>"})
    html = render_manifest_report_html(hostile)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_no_canary_pii_value_appears_in_json_or_html_report(
    built_manifest: ManifestEnvelope,
) -> None:
    json_text = canonical_bytes(build_manifest_report(built_manifest)).decode("utf-8")
    html_text = render_manifest_report_html(built_manifest)
    for canary in _CANARY_VALUES:
        assert canary not in json_text, f"raw canary value leaked into report JSON: {canary!r}"
        assert canary not in html_text, f"raw canary value leaked into report HTML: {canary!r}"
