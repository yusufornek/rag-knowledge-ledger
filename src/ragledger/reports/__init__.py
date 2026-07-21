"""MANIFEST/SNAPSHOT/reconciliation report generation, per PROJECT_SPEC.md section 23.

`ragledger.reports.manifest_report` builds a JSON-serializable summary
dict from a `ManifestEnvelope` (`build_manifest_report`) and renders it
as a self-contained HTML page (`render_manifest_report_html`).
`ragledger.reports.snapshot_report` does the same for an `.ndjson.zst`
snapshot file's header/trailer/point statistics, streaming the point
sequence rather than materializing it.
`ragledger.reports.reconciliation_report` renders an already-built
`ragledger.reconcile.report.ReconciliationReport` (JSON comes from that
package's own `to_json_bytes`; this module only adds the HTML view).

Every report's PII section carries only what `PiiFinding` itself already
carries -- entity type, confidence/count, and `masked_preview` -- never
a raw value; see `ragledger.governance.pii` for why that field is safe
to surface. No report in this package makes a network call, embeds
externally fetched content, or runs script content in its HTML output.
"""

from __future__ import annotations

from ragledger.reports.manifest_report import build_manifest_report, render_manifest_report_html
from ragledger.reports.reconciliation_report import render_reconciliation_report_html
from ragledger.reports.snapshot_report import build_snapshot_report, render_snapshot_report_html

__all__ = [
    "build_manifest_report",
    "build_snapshot_report",
    "render_manifest_report_html",
    "render_reconciliation_report_html",
    "render_snapshot_report_html",
]
