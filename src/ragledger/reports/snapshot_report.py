"""Build and render a SNAPSHOT summary report, per PROJECT_SPEC.md section 23.

Reads an `.ndjson.zst` snapshot file's header, streams every point to
compute bounded aggregate statistics, and reads the trailer -- never
holding the full point list in memory or in the report. Per section
23's "embedded data size cap" note, only aggregate counts and a small,
fixed-size sample of `raw_locator` values are ever included -- never a
full point dump.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ragledger.connectors.ndjson import SnapshotReader
from ragledger.reports._html import escape, page, stat_grid, table

_SAMPLE_LOCATOR_LIMIT = 10


def build_snapshot_report(path: Path) -> dict[str, Any]:
    """Summarize the snapshot at ``path`` into a plain, JSON-serializable report dict."""
    with SnapshotReader(path) as reader:
        header = reader.header
        vector_name_counts: Counter[str] = Counter()
        tenant_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()
        sample_locators: list[str] = []
        observed_count = 0
        for point in reader.points():
            observed_count += 1
            for name in point.vector_names:
                vector_name_counts[name] += 1
            if point.tenant is not None:
                tenant_counts[point.tenant] += 1
            for warning in point.normalization_warnings:
                warning_counts[warning] += 1
            if len(sample_locators) < _SAMPLE_LOCATOR_LIMIT:
                sample_locators.append(point.raw_locator)
        trailer = reader.trailer

    return {
        "report_type": "ragledger.snapshot_report.v1",
        "target_id": header.target_id,
        "scope": header.scope,
        "target_type": header.target_type,
        "vector_names": header.vector_names,
        "vector_dimensions": header.vector_dimensions,
        "started_at": header.started_at.isoformat(),
        "finished_at": trailer.finished_at.isoformat(),
        "connector_version": header.connector_version,
        "consistency_mode": header.consistency_mode,
        "snapshot_kind": header.snapshot_kind,
        "point_count": trailer.point_count,
        "observed_point_count": observed_count,
        "consistency_completeness": trailer.consistency_completeness,
        "consistency_start_count": trailer.consistency_start_count,
        "consistency_end_count": trailer.consistency_end_count,
        "content_hash": trailer.content_hash,
        "vector_name_point_counts": dict(sorted(vector_name_counts.items())),
        "tenant_point_counts": dict(sorted(tenant_counts.items())),
        "normalization_warning_counts": dict(sorted(warning_counts.items())),
        "sample_locators": sample_locators,
    }


def render_snapshot_report_html(path: Path) -> str:
    """Render the snapshot at ``path``'s summary report as a self-contained HTML page."""
    report = build_snapshot_report(path)
    subtitle = (
        f"target {report['target_id']} | scope {report['scope']} | type {report['target_type']} | "
        f"finished {report['finished_at']}"
    )
    completeness_class = (
        "status-ok" if report["consistency_completeness"] == "complete" else "status-warn"
    )
    summary_stats = stat_grid(
        [
            ("Points", report["point_count"]),
            ("Vectors", ", ".join(report["vector_names"]) or "-"),
            ("Consistency mode", report["consistency_mode"]),
        ]
    )
    vector_dimensions = report["vector_dimensions"]
    vector_counts = report["vector_name_point_counts"]

    sections = [
        f"<h2>Summary</h2>{summary_stats}"
        f'<p>Consistency: <span class="{completeness_class}">'
        f"{escape(report['consistency_completeness'])}</span>"
        f" (start={escape(report['consistency_start_count'])}, "
        f"end={escape(report['consistency_end_count'])})</p>",
        "<h2>Vector fields</h2>"
        + table(
            ["Name", "Dimension", "Points carrying it"],
            [
                (name, vector_dimensions.get(name, "-"), vector_counts.get(name, 0))
                for name in report["vector_names"]
            ],
        ),
        "<h2>Tenants observed</h2>"
        + table(["Tenant", "Points"], sorted(report["tenant_point_counts"].items())),
        "<h2>Normalization warnings</h2>"
        + table(["Code", "Points"], sorted(report["normalization_warning_counts"].items())),
        "<h2>Sample point locators</h2>"
        + table(["Locator"], [(locator,) for locator in report["sample_locators"]]),
        f"<h2>Integrity</h2><p>Content hash: <code>{escape(report['content_hash'])}</code></p>",
    ]
    return page(f"ragledger snapshot report: {report['target_id']}", subtitle, "".join(sections))
