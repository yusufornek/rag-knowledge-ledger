"""Policy v1 loading and evaluation, per PROJECT_SPEC.md sections 8.14 and 12.4.

`docs/spec/policy-v1.schema.json` was drafted in milestone M0 as a design
artifact ("not yet enforced by any CLI command", per the schema's own
`description`); this module is where it becomes load-bearing:
`load_policy_document` validates a raw YAML/JSON document against that exact
schema file (FR-130: unknown key is a hard error) before parsing it into the
typed `PolicyDocument` model below, and `evaluate_policy` turns a loaded
policy plus a `ragledger.reconcile.report.ReconciliationResult` into a
`PolicyVerdict`.

Schema gap, noted honestly rather than silently worked around (see
`docs/reviews/m6-status-notes.md` for the full writeup): the schema's
`$defs.rule` object (FR-131's "typed rules... category count/ratio,
severity, source path/media/license/PII/ACL/tenant, age, completeness") has
no field naming WHICH ratio a `category: "ratio"` rule checks, and no
severity-value field for a `category: "severity"` rule beyond `threshold`.
`_evaluate_rule` below implements the only two categories the schema
actually supports evaluating without guessing an unwritten convention:
"count" (matching findings, optionally filtered by `taxonomy_codes`,
compared to `threshold`) and "severity" (same, but `threshold` is read as a
minimum severity rank 0..3). Every other category degrades to the same
count-based evaluation, which is honest given the schema does not specify
enough per-category structure to do more.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from pydantic import Field

from ragledger.core.models import RagledgerModel
from ragledger.reconcile.report import PolicyVerdict, ReconciliationResult, RuleResult
from ragledger.reconcile.taxonomy import FindingCode, FindingSeverity

__all__ = [
    "AccessPolicy",
    "DriftPolicy",
    "FindingsPolicy",
    "LicensesPolicy",
    "PiiPolicy",
    "PolicyDocument",
    "PolicyRule",
    "PolicyValidationError",
    "Requirements",
    "evaluate_policy",
    "load_policy_document",
    "validate_policy_document",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA_PATH = _REPO_ROOT / "docs" / "spec" / "policy-v1.schema.json"

_SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_VERDICT_RANK: dict[str, int] = {"FAIL": 0, "INCONCLUSIVE": 1, "WARN": 2, "PASS": 3}


class PolicyValidationError(ValueError):
    """Raised for a policy document that fails schema or model validation."""


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema: dict[str, Any] = json.load(handle)
    return schema


def validate_policy_document(data: Mapping[str, Any]) -> None:
    """Validate a raw policy JSON document against `policy-v1.schema.json`."""
    try:
        jsonschema.validate(instance=data, schema=_load_schema())
    except jsonschema.exceptions.ValidationError as exc:
        raise PolicyValidationError(str(exc)) from exc


# --------------------------------------------------------------------------
# Typed policy model (mirrors the schema field-for-field)
# --------------------------------------------------------------------------


class Requirements(RagledgerModel):
    manifest_signature: Literal["required", "optional"] | None = None
    full_snapshot: Literal["required", "optional"] | None = None
    lineage_coverage_min: float | None = Field(default=None, ge=0, le=1)


class FindingsPolicy(RagledgerModel):
    fail_on_severity: list[FindingSeverity]
    warn_on_severity: list[FindingSeverity] = Field(default_factory=list)


class PiiPolicy(RagledgerModel):
    deny: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)
    max_confidence_allowed: float | None = Field(default=None, ge=0, le=1)


class LicensesPolicy(RagledgerModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    unknown: Literal["fail", "warn", "allow"] | None = None


class AccessPolicy(RagledgerModel):
    acl_required: bool | None = None
    tenant_required: bool | None = None
    acl_compliance_min: float | None = Field(default=None, ge=0, le=1)


class DriftPolicy(RagledgerModel):
    stale_ratio_max: float | None = Field(default=None, ge=0, le=1)
    orphan_ratio_max: float | None = Field(default=None, ge=0, le=1)
    missing_ratio_max: float | None = Field(default=None, ge=0, le=1)


class PolicyRule(RagledgerModel):
    category: Literal[
        "count",
        "ratio",
        "severity",
        "source_path",
        "media_type",
        "license",
        "pii",
        "acl",
        "tenant",
        "age",
        "completeness",
    ]
    taxonomy_codes: list[FindingCode] | None = None
    comparator: Literal["lt", "lte", "gt", "gte", "eq"] | None = None
    threshold: float | None = None
    pattern: str | None = None
    max_age_days: int | None = Field(default=None, ge=0)
    verdict_on_violation: Literal["WARN", "FAIL"]


class PolicyDocument(RagledgerModel):
    version: Literal[1] = 1
    name: str
    requirements: Requirements
    findings: FindingsPolicy
    pii: PiiPolicy | None = None
    licenses: LicensesPolicy | None = None
    access: AccessPolicy | None = None
    drift: DriftPolicy | None = None
    rules: list[PolicyRule] = Field(default_factory=list)


def load_policy_document(
    text: str, *, document_format: Literal["yaml", "json"] = "yaml"
) -> PolicyDocument:
    """Load+validate a policy document from YAML or JSON text.

    Validates against `docs/spec/policy-v1.schema.json` first (FR-130:
    unknown key is a hard error at the schema level), then parses into
    `PolicyDocument`, whose own `extra="forbid"` fields are a second,
    independent guarantee of the same "no unknown key" rule.
    """
    data: Any = yaml.safe_load(text) if document_format == "yaml" else json.loads(text)
    if not isinstance(data, dict):
        raise PolicyValidationError("policy document must be a mapping at the top level")
    validate_policy_document(data)
    return PolicyDocument.model_validate(data)


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

_RecordFn = Callable[[str, str, str], None]


def _worse(first: str, second: str) -> str:
    return first if _VERDICT_RANK[first] <= _VERDICT_RANK[second] else second


def evaluate_policy(document: PolicyDocument, result: ReconciliationResult) -> PolicyVerdict:
    """Evaluate `document` against an already-produced `result`, per FR-131/FR-132.

    Deterministic and pure: never reads the wall clock, never mutates
    `result`. Overall verdict is the worst of every individual rule result,
    ranked FAIL > INCONCLUSIVE > WARN > PASS (an inconclusive rule -- a
    ratio that was "not applicable" for a gate the policy author explicitly
    configured -- is treated as more severe than a warning but less severe
    than a confirmed failure).
    """
    rule_results: list[RuleResult] = []
    overall = "PASS"

    def _record(name: str, verdict: str, detail: str) -> None:
        nonlocal overall
        rule_results.append(
            RuleResult(rule=name, passed=verdict == "PASS", verdict=verdict, detail=detail)
        )
        overall = _worse(overall, verdict)

    _evaluate_requirements(document, result, _record)
    _evaluate_findings_severity(document, result, _record)
    _evaluate_licenses(document, result, _record)
    _evaluate_access(document, result, _record)
    _evaluate_drift(document, result, _record)
    for index, rule in enumerate(document.rules):
        verdict, detail = _evaluate_rule(rule, result)
        _record(f"rules[{index}].{rule.category}", verdict, detail)

    return PolicyVerdict(policy_name=document.name, verdict=overall, rule_results=rule_results)


def _evaluate_requirements(
    document: PolicyDocument, result: ReconciliationResult, record: _RecordFn
) -> None:
    if document.requirements.manifest_signature == "required":
        if result.summary.manifest_signed:
            record(
                "requirements.manifest_signature", "PASS", "manifest carries at least one signature"
            )
        else:
            record(
                "requirements.manifest_signature",
                "FAIL",
                "manifest_signature is required but the manifest is unsigned",
            )

    if document.requirements.full_snapshot == "required":
        consistency = result.consistency
        if consistency.snapshot_kind == "full" and consistency.completeness == "complete":
            record(
                "requirements.full_snapshot", "PASS", "observed snapshot is a complete full pass"
            )
        else:
            record(
                "requirements.full_snapshot",
                "FAIL",
                f"full_snapshot is required but snapshot_kind={consistency.snapshot_kind!r} "
                f"completeness={consistency.completeness!r}",
            )

    if document.requirements.lineage_coverage_min is not None:
        minimum = document.requirements.lineage_coverage_min
        coverage = result.ratios.lineage_coverage
        if coverage is None:
            record(
                "requirements.lineage_coverage_min",
                "INCONCLUSIVE",
                "lineage_coverage is not applicable (zero observed points)",
            )
        elif coverage < minimum:
            record(
                "requirements.lineage_coverage_min",
                "FAIL",
                f"lineage_coverage {coverage:.4f} < required {minimum:.4f}",
            )
        else:
            record(
                "requirements.lineage_coverage_min",
                "PASS",
                f"lineage_coverage {coverage:.4f} >= required {minimum:.4f}",
            )


def _evaluate_findings_severity(
    document: PolicyDocument, result: ReconciliationResult, record: _RecordFn
) -> None:
    fail_severities = {severity.value for severity in document.findings.fail_on_severity}
    fail_count = sum(1 for finding in result.findings if finding.severity.value in fail_severities)
    if fail_count:
        record(
            "findings.fail_on_severity",
            "FAIL",
            f"{fail_count} finding(s) at a fail-gated severity {sorted(fail_severities)}",
        )
    else:
        record("findings.fail_on_severity", "PASS", "no findings at a fail-gated severity")

    warn_severities = {severity.value for severity in document.findings.warn_on_severity}
    if warn_severities:
        warn_count = sum(
            1 for finding in result.findings if finding.severity.value in warn_severities
        )
        if warn_count:
            record(
                "findings.warn_on_severity",
                "WARN",
                f"{warn_count} finding(s) at a warn-gated severity {sorted(warn_severities)}",
            )
        else:
            record("findings.warn_on_severity", "PASS", "no findings at a warn-gated severity")


def _evaluate_licenses(
    document: PolicyDocument, result: ReconciliationResult, record: _RecordFn
) -> None:
    if document.licenses is None or not document.licenses.unknown:
        return
    unknown_count = sum(
        1 for finding in result.findings if finding.code == FindingCode.LICENSE_UNKNOWN
    )
    if unknown_count and document.licenses.unknown != "allow":
        verdict = "FAIL" if document.licenses.unknown == "fail" else "WARN"
        record(
            "licenses.unknown",
            verdict,
            f"{unknown_count} source(s) with an unresolved (NOASSERTION) license",
        )
    else:
        record(
            "licenses.unknown",
            "PASS",
            "no unresolved-license findings, or unknown licenses are allowed",
        )


def _evaluate_access(
    document: PolicyDocument, result: ReconciliationResult, record: _RecordFn
) -> None:
    if document.access is None or document.access.acl_compliance_min is None:
        return
    minimum = document.access.acl_compliance_min
    compliance = result.ratios.acl_compliance
    if compliance is None:
        record(
            "access.acl_compliance_min",
            "INCONCLUSIVE",
            "acl_compliance is not applicable (no ACL-required matches)",
        )
    elif compliance < minimum:
        record(
            "access.acl_compliance_min",
            "FAIL",
            f"acl_compliance {compliance:.4f} < required {minimum:.4f}",
        )
    else:
        record(
            "access.acl_compliance_min",
            "PASS",
            f"acl_compliance {compliance:.4f} >= required {minimum:.4f}",
        )


def _evaluate_drift(
    document: PolicyDocument, result: ReconciliationResult, record: _RecordFn
) -> None:
    if document.drift is None:
        return
    _drift_rule(
        record, "drift.stale_ratio_max", result.ratios.stale_ratio, document.drift.stale_ratio_max
    )
    _drift_rule(
        record,
        "drift.orphan_ratio_max",
        result.ratios.orphan_ratio,
        document.drift.orphan_ratio_max,
    )
    _drift_rule(
        record,
        "drift.missing_ratio_max",
        result.ratios.missing_ratio,
        document.drift.missing_ratio_max,
    )


def _drift_rule(
    record: _RecordFn, name: str, value: float | None, max_allowed: float | None
) -> None:
    if max_allowed is None:
        return
    if value is None:
        # Not applicable (zero denominator): a drift ceiling silently does
        # not apply rather than forcing an INCONCLUSIVE verdict -- unlike
        # `lineage_coverage_min`/`acl_compliance_min`, an "N/A" drift ratio
        # (for example zero expected bindings at all) is not inherently
        # concerning. See this module's docstring for the distinction.
        return
    label = name.rsplit(".", 1)[-1]
    if value > max_allowed:
        record(name, "FAIL", f"{label} {value:.4f} exceeds max {max_allowed:.4f}")
    else:
        record(name, "PASS", f"{label} {value:.4f} within max {max_allowed:.4f}")


_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
}


def _evaluate_rule(rule: PolicyRule, result: ReconciliationResult) -> tuple[str, str]:
    findings = result.findings
    if rule.taxonomy_codes:
        codes = set(rule.taxonomy_codes)
        findings = [finding for finding in findings if finding.code in codes]
    if rule.category == "severity" and not rule.taxonomy_codes and rule.threshold is not None:
        minimum_rank = int(rule.threshold)
        findings = [
            finding
            for finding in findings
            if _SEVERITY_RANK[finding.severity.value] >= minimum_rank
        ]

    value = float(len(findings))
    comparator_name = rule.comparator or "gte"
    threshold = rule.threshold if rule.threshold is not None else 0.0
    comparator = _COMPARATORS[comparator_name]
    violated = comparator(value, threshold)
    verdict = rule.verdict_on_violation if violated else "PASS"
    detail = f"{len(findings)} matching finding(s), threshold {comparator_name} {threshold}"
    return verdict, detail
