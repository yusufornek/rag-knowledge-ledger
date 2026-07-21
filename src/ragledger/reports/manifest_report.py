"""Build and render a MANIFEST summary report, per PROJECT_SPEC.md section 23.

`build_manifest_report` produces a plain, canonical-JSON-serializable
dict (only `str`/`int`/`float`/`bool`/`None`/`list`/`dict` values -- no
`datetime` objects, no pydantic models), so it round-trips through
`ragledger.core.canonical.canonical_bytes` unchanged for `--format
json`. `render_manifest_report_html` renders that same dict as a
self-contained HTML page, so the two output formats are guaranteed to
describe identical facts -- there is only one code path that reads the
`ManifestEnvelope`.

The governance section's only PII evidence is what
`ragledger.governance.pii.PiiFinding` itself already carries -- entity
type, a count, and an already-masked preview string -- never a raw
value or an unmasked substring; see that module's docstring for why
`masked_preview` is safe to surface here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ragledger.core.models import (
    AclAssertion,
    LicenseAssertion,
    ManifestEnvelope,
    PiiScanAssertion,
    QualityAssertion,
    TenantAssertion,
)
from ragledger.reports._html import escape, page, stat_grid, table


@dataclass
class _Facts:
    source_media_types: Counter[str] = field(default_factory=Counter)
    source_status: Counter[str] = field(default_factory=Counter)
    parse_status: Counter[str] = field(default_factory=Counter)
    parse_by_parser: Counter[str] = field(default_factory=Counter)
    embedding_models: Counter[str] = field(default_factory=Counter)
    pii_status: Counter[str] = field(default_factory=Counter)
    pii_entity_types: Counter[str] = field(default_factory=Counter)
    pii_examples: dict[str, str] = field(default_factory=dict)
    license_expressions: Counter[str] = field(default_factory=Counter)
    acl_entry_sets: Counter[str] = field(default_factory=Counter)
    tenant_values: Counter[str] = field(default_factory=Counter)
    warning_codes: Counter[str] = field(default_factory=Counter)


def _collect_facts(manifest: ManifestEnvelope) -> _Facts:
    facts = _Facts()
    for source in manifest.sources:
        facts.source_media_types[source.media_type] += 1
        facts.source_status[source.status] += 1
    for run in manifest.parse_runs:
        facts.parse_status[run.status] += 1
        facts.parse_by_parser[run.parser_name] += 1
        for warning in run.warnings:
            facts.warning_codes[warning.code] += 1
    for embedding in manifest.embeddings:
        facts.embedding_models[f"{embedding.model.provider}/{embedding.model.name}"] += 1
    for warning in manifest.build.warnings:
        facts.warning_codes[warning.code] += 1

    license_by_id: dict[str, LicenseAssertion] = {}
    acl_by_id: dict[str, AclAssertion] = {}

    for assertion in manifest.assertions:
        if isinstance(assertion, PiiScanAssertion):
            facts.pii_status[assertion.status] += 1
            for finding in assertion.findings:
                facts.pii_entity_types[finding.entity_type] += 1
                if finding.entity_type not in facts.pii_examples and finding.masked_preview:
                    facts.pii_examples[finding.entity_type] = finding.masked_preview
        elif isinstance(assertion, LicenseAssertion):
            license_by_id[assertion.id] = assertion
        elif isinstance(assertion, AclAssertion):
            acl_by_id[assertion.id] = assertion
        elif isinstance(assertion, TenantAssertion):
            pass  # joined below, via `source.declared_tenant`
        elif isinstance(assertion, QualityAssertion):
            for warning in assertion.warnings:
                facts.warning_codes[warning.code] += 1

    for source in manifest.sources:
        for assertion_id in source.license_assertion_ids:
            license_assertion = license_by_id.get(assertion_id)
            if license_assertion is not None:
                facts.license_expressions[license_assertion.spdx_expression] += 1
        if source.declared_acl_assertion_id is not None:
            acl_assertion = acl_by_id.get(source.declared_acl_assertion_id)
            if acl_assertion is not None:
                facts.acl_entry_sets[",".join(acl_assertion.entries) or "(empty)"] += 1
        if source.declared_tenant is not None:
            facts.tenant_values[source.declared_tenant] += 1

    return facts


def build_manifest_report(manifest: ManifestEnvelope) -> dict[str, Any]:
    """Summarize ``manifest`` into a plain, JSON-serializable report dict."""
    facts = _collect_facts(manifest)
    return {
        "report_type": "ragledger.manifest_report.v1",
        "namespace": manifest.namespace,
        "ledger_version": manifest.ledger_version,
        "created_at": manifest.created_at.isoformat(),
        "build": {
            "build_id": manifest.build.build_id,
            "status": manifest.build.status,
            "started_at": manifest.build.started_at.isoformat(),
            "completed_at": manifest.build.completed_at.isoformat(),
            "environment": {
                "os": manifest.build.environment.os,
                "python_version": manifest.build.environment.python_version,
            },
            "stages": [
                {
                    "name": stage.name,
                    "version": stage.version,
                    "input_count": stage.input_count,
                    "output_count": stage.output_count,
                }
                for stage in manifest.build.stages
            ],
        },
        "statistics": {
            "source_count": manifest.statistics.source_count,
            "source_version_count": manifest.statistics.source_version_count,
            "parse_run_count": manifest.statistics.parse_run_count,
            "chunk_count": manifest.statistics.chunk_count,
            "embedding_count": manifest.statistics.embedding_count,
            "index_binding_count": manifest.statistics.index_binding_count,
            "assertion_count": manifest.statistics.assertion_count,
            "artifact_count": manifest.statistics.artifact_count,
            "warning_count": manifest.statistics.warning_count,
        },
        "sources": {
            "by_media_type": dict(sorted(facts.source_media_types.items())),
            "by_status": dict(sorted(facts.source_status.items())),
        },
        "parsing": {
            "by_status": dict(sorted(facts.parse_status.items())),
            "by_parser": dict(sorted(facts.parse_by_parser.items())),
        },
        "embeddings": {"by_model": dict(sorted(facts.embedding_models.items()))},
        "governance": {
            "pii": {
                "assertion_status_counts": dict(sorted(facts.pii_status.items())),
                "findings_by_entity_type": dict(sorted(facts.pii_entity_types.items())),
                "masked_examples_by_entity_type": dict(sorted(facts.pii_examples.items())),
            },
            "license": {
                "effective_expression_counts": dict(sorted(facts.license_expressions.items())),
            },
            "acl": {"entry_set_counts": dict(sorted(facts.acl_entry_sets.items()))},
            "tenant": {"value_counts": dict(sorted(facts.tenant_values.items()))},
        },
        "warnings": {"by_code": dict(sorted(facts.warning_codes.items()))},
        "integrity": {
            "canonicalization": manifest.integrity.canonicalization,
            "hash_algorithm": manifest.integrity.hash_algorithm,
            "manifest_hash": manifest.integrity.manifest_hash,
        },
        "signatures": [
            {
                "key_id": sig.key_id,
                "algorithm": sig.algorithm,
                "issuer": sig.issuer,
                "signed_at": sig.signed_at.isoformat(),
            }
            for sig in manifest.signatures
        ],
    }


_STATUS_CLASS = {"complete": "status-ok", "incomplete": "status-warn", "cancelled": "status-fail"}


def render_manifest_report_html(manifest: ManifestEnvelope) -> str:
    """Render ``manifest``'s summary report as a self-contained HTML page."""
    report = build_manifest_report(manifest)
    build_info = report["build"]
    stats = report["statistics"]
    subtitle = (
        f"namespace {report['namespace']} | build {build_info['build_id']} | "
        f"status {build_info['status']} | created {report['created_at']}"
    )
    status_class = _STATUS_CLASS.get(build_info["status"], "status-warn")

    summary_stats = stat_grid(
        [
            ("Sources", stats["source_count"]),
            ("Chunks", stats["chunk_count"]),
            ("Embeddings", stats["embedding_count"]),
            ("Assertions", stats["assertion_count"]),
            ("Artifacts", stats["artifact_count"]),
            ("Warnings", stats["warning_count"] or 0),
        ]
    )
    pii = report["governance"]["pii"]
    pii_findings_rows = [
        (entity, count, pii["masked_examples_by_entity_type"].get(entity, ""))
        for entity, count in sorted(pii["findings_by_entity_type"].items())
    ]

    sections = [
        f"<h2>Summary</h2>{summary_stats}"
        f'<p>Build status: <span class="{status_class}">{escape(build_info["status"])}</span></p>',
        "<h2>Sources</h2>"
        + table(["Media type", "Count"], sorted(report["sources"]["by_media_type"].items()))
        + table(["Status", "Count"], sorted(report["sources"]["by_status"].items())),
        "<h2>Parsing</h2>"
        + table(["Status", "Count"], sorted(report["parsing"]["by_status"].items()))
        + table(["Parser", "Count"], sorted(report["parsing"]["by_parser"].items())),
        "<h2>Embeddings</h2>"
        + table(["Model", "Count"], sorted(report["embeddings"]["by_model"].items())),
        "<h2>Governance: PII</h2>"
        + table(["Scan status", "Count"], sorted(pii["assertion_status_counts"].items()))
        + table(["Entity type", "Findings", "Masked example"], pii_findings_rows),
        "<h2>Governance: license</h2>"
        + table(
            ["Effective SPDX expression", "Sources"],
            sorted(report["governance"]["license"]["effective_expression_counts"].items()),
        ),
        "<h2>Governance: ACL and tenant</h2>"
        + table(
            ["ACL entry set", "Sources"],
            sorted(report["governance"]["acl"]["entry_set_counts"].items()),
        )
        + table(
            ["Tenant", "Sources"], sorted(report["governance"]["tenant"]["value_counts"].items())
        ),
        "<h2>Warnings</h2>"
        + table(["Code", "Count"], sorted(report["warnings"]["by_code"].items())),
        "<h2>Integrity and signatures</h2>"
        f"<p>Manifest hash (<code>{escape(report['integrity']['hash_algorithm'])}</code> over "
        f"<code>{escape(report['integrity']['canonicalization'])}</code> canonical bytes): "
        f"<code>{escape(report['integrity']['manifest_hash'])}</code></p>"
        + table(
            ["Key id", "Algorithm", "Issuer", "Signed at"],
            [
                (sig["key_id"], sig["algorithm"], sig["issuer"] or "-", sig["signed_at"])
                for sig in report["signatures"]
            ],
        ),
    ]
    return page(f"ragledger manifest report: {report['namespace']}", subtitle, "".join(sections))
