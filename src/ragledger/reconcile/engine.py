"""Streaming reconciliation engine, per the design specification section 14.

Two entry points share every finding-construction rule below them:

- `reconcile_small_data`: section 14.1's small-data path. Materializes the
  expected and observed point sets in memory (bounded by
  `max_in_memory_points`) and matches them via
  `ragledger.reconcile.matching.match_all_levels`.
- `reconcile_big_data`: section 14.2's big-data path. Spills expected
  binding records AND the observed connector stream to sorted temp run
  files under a caller-supplied work directory (0700, cleaned up by this
  module -- never left for the caller to manage), and matches them via the
  same `ragledger.reconcile.matching.stream_merge_join` primitive fed by a
  `heapq.merge` of those run files instead of an in-memory `sorted()` list.

Both paths call the exact same `_finalize`/`_compare_matched_pair`/
`_classify_missing`/`_classify_orphan` helpers on whatever `MatchOutcome`
they produced, which is what makes their findings identical for the same
logical input (`tests/reconcile/test_engine_equivalence.py`).

Section 14.3's comparison order is followed exactly: target schema (the
preflight short-circuit below) -> point set (missing/orphan) -> identity
lineage (implicit in how a `MatchedPair` was formed) -> source/parse/chunk/
embedding versions (staleness) -> payload projection (drift) -> ACL/tenant ->
vector hash (optional) -> PII/license policy facts from the manifest.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from ragledger.connectors.base import (
    NormalizedPoint,
    SnapshotCompleteness,
    TargetSchema,
    VectorTargetConnector,
)
from ragledger.core.models import (
    Assertion,
    LicenseAssertion,
    ManifestEnvelope,
    PiiFinding,
    PiiScanAssertion,
    SourceRecord,
)
from ragledger.reconcile import matching
from ragledger.reconcile.matching import (
    ExpectedPoint,
    MatchedPair,
    MatchLevel,
    MatchOutcome,
    normalize_point_id,
    resolve_expected_points,
)
from ragledger.reconcile.policy import PiiPolicy, PolicyDocument
from ragledger.reconcile.report import (
    ConsistencyCaveat,
    Ratios,
    ReconciliationResult,
    Summary,
    consistency_caveat_from_info,
)
from ragledger.reconcile.report import ratio as _ratio
from ragledger.reconcile.taxonomy import (
    AffectedLineage,
    Finding,
    FindingCode,
    FindingSeverity,
    build_finding,
    mask_acl_entries,
)

__all__ = [
    "reconcile_big_data",
    "reconcile_small_data",
]

_STALE_CODES = frozenset(
    {FindingCode.STALE_SOURCE, FindingCode.STALE_PARSE, FindingCode.STALE_CHUNKING}
)
_ACL_VIOLATION_CODES = frozenset(
    {FindingCode.ACL_MISSING, FindingCode.ACL_BROADER_THAN_SOURCE, FindingCode.ACL_MISMATCH}
)
_LICENSE_PRECEDENCE: dict[str, int] = {
    "user_assertion": 0,
    "sidecar": 1,
    "frontmatter": 2,
    "path_rule": 3,
    "repository_default": 4,
}
_NEXT_CLOSING_LEVEL: dict[MatchLevel, MatchLevel] = {
    MatchLevel.POINT_ID: MatchLevel.EMBEDDING_ID,
    MatchLevel.EMBEDDING_ID: MatchLevel.CHUNK_ID,
}


# ==========================================================================
# Small-data path (section 14.1)
# ==========================================================================


def reconcile_small_data(
    manifest: ManifestEnvelope,
    connector: VectorTargetConnector[Any],
    *,
    target: str,
    scope: str,
    policy: PolicyDocument | None = None,
    workspace_secret: bytes | None = None,
    snapshot_kind: Literal["full", "sample"] = "full",
    max_in_memory_points: int = 100_000,
) -> ReconciliationResult:
    """Reconcile one (target, scope) entirely in memory (section 14.1).

    `max_in_memory_points` is a guard rail, not a streaming bound (this path
    always fully materializes `connector.iterate_points()`): use
    `reconcile_big_data` for point counts beyond the low hundred-thousands.
    """
    expected_points = resolve_expected_points(manifest, target=target, scope=scope)
    target_schema = connector.inspect_target_schema()
    preflight = _preflight_schema_check(expected_points, target_schema, target=target, scope=scope)
    if preflight:
        return _short_circuit_result(manifest, target, scope, preflight, snapshot_kind)

    observed_points = list(connector.iterate_points())
    if len(observed_points) > max_in_memory_points:
        raise ValueError(
            f"{len(observed_points)} observed points exceeds max_in_memory_points="
            f"{max_in_memory_points}; use reconcile_big_data for this scale"
        )
    consistency = connector.get_consistency_info()

    outcome = matching.match_all_levels(expected_points, observed_points)
    return _finalize(
        manifest=manifest,
        target=target,
        scope=scope,
        policy=policy,
        workspace_secret=workspace_secret,
        snapshot_kind=snapshot_kind,
        expected_points=expected_points,
        observed_count=len(observed_points),
        outcome=outcome,
        consistency_completeness=consistency.completeness,
        consistency_caveat=consistency_caveat_from_info(consistency, snapshot_kind=snapshot_kind),
    )


# ==========================================================================
# Big-data path (section 14.2): external sort/merge, bounded memory
# ==========================================================================


class _ChunkedRunWriter:
    """Buffers up to `chunk_size` items, sorts, and spills one sorted run
    file at a time -- the external-sort half of section 14.2's algorithm.

    Never holds more than `chunk_size` items in memory regardless of how
    many items pass through `add`, which is what makes leftovers between
    matching rounds bounded-memory too (a round's leftover callback feeds a
    writer directly, not a Python list).
    """

    def __init__(
        self,
        work_dir: Path,
        prefix: str,
        chunk_size: int,
        key_fn: Callable[[Any], Any],
        serialize: Callable[[Any], bytes],
    ) -> None:
        self._work_dir = work_dir
        self._prefix = prefix
        self._chunk_size = chunk_size
        self._key_fn = key_fn
        self._serialize = serialize
        self._buffer: list[Any] = []
        self._runs: list[Path] = []

    def add(self, item: Any) -> None:
        self._buffer.append(item)
        if len(self._buffer) >= self._chunk_size:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        self._buffer.sort(key=self._key_fn)
        run_path = self._work_dir / f"{self._prefix}-{len(self._runs):06d}-{uuid.uuid4().hex}.jsonl"
        with run_path.open("wb") as handle:
            for item in self._buffer:
                handle.write(self._serialize(item))
                handle.write(b"\n")
        self._runs.append(run_path)
        self._buffer = []

    def finish(self) -> list[Path]:
        self._flush()
        return self._runs


def _serialize_normalized_point(point: NormalizedPoint) -> bytes:
    """Encode one `NormalizedPoint` to a spill-run-file line.

    `model_dump_json` (pydantic-core's own fast serializer), not
    `ragledger.core.canonical.canonical_bytes` -- see
    `ragledger.reconcile.matching.expected_point_to_json_bytes`'s docstring
    for why a spill run file never needs RFC 8785 canonical bytes.
    """
    return point.model_dump_json(exclude_none=True).encode("utf-8")


def _deserialize_normalized_point(data: bytes) -> NormalizedPoint:
    return NormalizedPoint.model_validate_json(data)


def _iter_run(path: Path, deserialize: Callable[[bytes], Any]) -> Iterator[Any]:
    with path.open("rb") as handle:
        for line in handle:
            stripped = line.rstrip(b"\n")
            if stripped:
                yield deserialize(stripped)


def _merge_runs(
    paths: Sequence[Path], key_fn: Callable[[Any], Any], deserialize: Callable[[bytes], Any]
) -> Iterator[Any]:
    import heapq

    return heapq.merge(*(_iter_run(path, deserialize) for path in paths), key=key_fn)


def _reset_work_dir(work_dir: Path) -> None:
    """Clear every entry under `work_dir`, creating it (mode 0700) if missing.

    Called both at the START of `reconcile_big_data` (idempotent restart:
    sweeps any spill files a previous, crashed attempt left behind under
    this same caller-supplied work directory) and at the END of a
    successful run (self-cleanup). Deliberately NOT called from a
    `finally` block wrapping the whole run: if this run itself is
    interrupted (an exception mid-merge), its spill files are left in place
    on disk rather than erased, so the only cleanup path is the next
    attempt's start-of-run sweep -- this is what "cancel-safe,
    checkpoint/restart idempotent" means in practice (section 14.2:
    "Temp workspace unique... cleanup cancel/failure").
    """
    work_dir = Path(work_dir)
    if work_dir.exists():
        for entry in work_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    else:
        work_dir.mkdir(parents=True)
    work_dir.chmod(0o700)


def reconcile_big_data(
    manifest: ManifestEnvelope,
    connector: VectorTargetConnector[Any],
    *,
    target: str,
    scope: str,
    work_dir: Path,
    policy: PolicyDocument | None = None,
    workspace_secret: bytes | None = None,
    snapshot_kind: Literal["full", "sample"] = "full",
    chunk_size: int = 20_000,
) -> ReconciliationResult:
    """Reconcile one (target, scope) via external sort/merge (section 14.2).

    `work_dir` is a caller-supplied, run-exclusive temp directory (never
    shared with a concurrent run): this function owns everything under it.
    Bounded memory: at any instant, at most `chunk_size` expected points,
    `chunk_size` observed points, and one key-group's worth of matched-key
    duplicates are held in memory; run files are read back via `heapq.merge`
    generators, never loaded whole.
    """
    expected_points = resolve_expected_points(manifest, target=target, scope=scope)
    target_schema = connector.inspect_target_schema()
    preflight = _preflight_schema_check(expected_points, target_schema, target=target, scope=scope)
    if preflight:
        return _short_circuit_result(manifest, target, scope, preflight, snapshot_kind)

    work_dir = Path(work_dir)
    _reset_work_dir(work_dir)

    matched: list[MatchedPair] = []
    duplicate_expected: list[tuple[MatchLevel, str, list[ExpectedPoint]]] = []
    duplicate_observed: list[tuple[MatchLevel, str, list[NormalizedPoint]]] = []

    level = MatchLevel.POINT_ID
    expected_writer = _ChunkedRunWriter(
        work_dir,
        "expected-r1",
        chunk_size,
        matching.expected_sort_key(level),
        matching.expected_point_to_json_bytes,
    )
    for expected_point in expected_points:
        expected_writer.add(expected_point)
    expected_runs = expected_writer.finish()

    observed_writer = _ChunkedRunWriter(
        work_dir,
        "observed-r1",
        chunk_size,
        matching.observed_sort_key(level),
        _serialize_normalized_point,
    )
    observed_count = 0
    for observed_point in connector.iterate_points():
        observed_writer.add(observed_point)
        observed_count += 1
    observed_runs = observed_writer.finish()

    final_missing: list[ExpectedPoint] = []
    final_orphan: list[NormalizedPoint] = []

    for round_number, level in enumerate(matching.CLOSING_LEVELS, start=1):
        expected_stream = _merge_runs(
            expected_runs,
            matching.expected_sort_key(level),
            matching.expected_point_from_json_bytes,
        )
        observed_stream = _merge_runs(
            observed_runs, matching.observed_sort_key(level), _deserialize_normalized_point
        )

        is_last_round = level is MatchLevel.CHUNK_ID
        if is_last_round:
            matching.stream_merge_join(
                level,
                expected_stream,
                observed_stream,
                on_matched=matched.append,
                on_leftover_expected=final_missing.append,
                on_leftover_observed=final_orphan.append,
                on_duplicate_expected=lambda lvl, key, group: duplicate_expected.append(
                    (lvl, key, group)
                ),
                on_duplicate_observed=lambda lvl, key, group: duplicate_observed.append(
                    (lvl, key, group)
                ),
            )
        else:
            next_level = _NEXT_CLOSING_LEVEL[level]
            next_expected_writer = _ChunkedRunWriter(
                work_dir,
                f"expected-r{round_number + 1}",
                chunk_size,
                matching.expected_sort_key(next_level),
                matching.expected_point_to_json_bytes,
            )
            next_observed_writer = _ChunkedRunWriter(
                work_dir,
                f"observed-r{round_number + 1}",
                chunk_size,
                matching.observed_sort_key(next_level),
                _serialize_normalized_point,
            )
            matching.stream_merge_join(
                level,
                expected_stream,
                observed_stream,
                on_matched=matched.append,
                on_leftover_expected=next_expected_writer.add,
                on_leftover_observed=next_observed_writer.add,
                on_duplicate_expected=lambda lvl, key, group: duplicate_expected.append(
                    (lvl, key, group)
                ),
                on_duplicate_observed=lambda lvl, key, group: duplicate_observed.append(
                    (lvl, key, group)
                ),
            )

        for path in expected_runs:
            path.unlink(missing_ok=True)
        for path in observed_runs:
            path.unlink(missing_ok=True)

        if not is_last_round:
            expected_runs = next_expected_writer.finish()
            observed_runs = next_observed_writer.finish()

    consistency = connector.get_consistency_info()
    relocation, orphan_candidates = matching.relocation_suggestions(final_missing, final_orphan)
    outcome = MatchOutcome(
        matched=matched,
        missing_expected=final_missing,
        orphan_observed=final_orphan,
        duplicate_expected_groups=duplicate_expected,
        duplicate_observed_groups=duplicate_observed,
        relocation_candidates=relocation,
        orphan_candidates=orphan_candidates,
    )
    result = _finalize(
        manifest=manifest,
        target=target,
        scope=scope,
        policy=policy,
        workspace_secret=workspace_secret,
        snapshot_kind=snapshot_kind,
        expected_points=expected_points,
        observed_count=observed_count,
        outcome=outcome,
        consistency_completeness=consistency.completeness,
        consistency_caveat=consistency_caveat_from_info(consistency, snapshot_kind=snapshot_kind),
    )
    _reset_work_dir(work_dir)
    return result


# ==========================================================================
# Preflight (section 14.3's "target schema" comparison, scenario C)
# ==========================================================================


def _preflight_schema_check(
    expected_points: Sequence[ExpectedPoint],
    target_schema: TargetSchema,
    *,
    target: str,
    scope: str,
) -> list[Finding]:
    """A cheap target-schema check run BEFORE any point is streamed.

    Acceptance scenario C: "gereksiz full vector scan başlamadan fail" (fail
    before an unnecessary full vector scan) -- when the manifest's expected
    embedding dimensions and the target's actual vector-field dimensions
    have no overlap at all, reconciliation cannot possibly succeed for any
    point in this scope, so it short-circuits here instead of iterating the
    whole target.
    """
    expected_dimensions = {
        point.embedding_dimension
        for point in expected_points
        if point.embedding_dimension is not None
    }
    schema_dimensions = {field.dimension for field in target_schema.vector_fields}
    if expected_dimensions and schema_dimensions and not (expected_dimensions & schema_dimensions):
        subject = f"{target}:{scope}"
        return [
            build_finding(
                code=FindingCode.EMBEDDING_DIMENSION_MISMATCH,
                target=target,
                scope=scope,
                subject_id=subject,
                affected_field="dimension",
                evidence={
                    "expected_dimensions": sorted(expected_dimensions),
                    "target_dimensions": sorted(schema_dimensions),
                },
                detail="target vector dimension(s) share no value with any expected dimension",
            )
        ]
    return []


def _short_circuit_result(
    manifest: ManifestEnvelope,
    target: str,
    scope: str,
    findings: list[Finding],
    snapshot_kind: str,
) -> ReconciliationResult:
    findings = sorted(findings, key=lambda finding: finding.fingerprint)
    severity_counts = _severity_counts(findings)
    summary = Summary(
        target=target,
        scope=scope,
        expected_bindings=0,
        observed_points=0,
        matched_points=0,
        finding_count=len(findings),
        finding_count_by_severity=severity_counts,
        manifest_signed=len(manifest.signatures) > 0,
        preflight_short_circuited=True,
    )
    caveat = ConsistencyCaveat(
        mode="best_effort_live",
        completeness="incomplete",
        start_count=None,
        end_count=None,
        observed_count=0,
        degraded_confidence=True,
        snapshot_kind=snapshot_kind,
        detail="preflight target-schema check failed; the full snapshot was never read",
    )
    return ReconciliationResult(
        summary=summary,
        ratios=Ratios(),
        findings=findings,
        consistency=caveat,
        manifest_id=manifest.build.build_id,
        manifest_status=manifest.build.status,
    )


def _severity_counts(findings: Sequence[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    return counts


# ==========================================================================
# Shared finalization: findings from a MatchOutcome, plus ratios/summary
# ==========================================================================


def _finalize(
    *,
    manifest: ManifestEnvelope,
    target: str,
    scope: str,
    policy: PolicyDocument | None,
    workspace_secret: bytes | None,
    snapshot_kind: str,
    expected_points: Sequence[ExpectedPoint],
    observed_count: int,
    outcome: MatchOutcome,
    consistency_completeness: SnapshotCompleteness,
    consistency_caveat: ConsistencyCaveat,
) -> ReconciliationResult:
    sources_by_id = {source.id: source for source in manifest.sources}
    assertions_by_id: dict[str, Assertion] = {
        assertion.id: assertion for assertion in manifest.assertions
    }

    findings: list[Finding] = []
    stale_count = 0
    acl_required_count = 0
    acl_compliant_count = 0

    for pair in outcome.matched:
        pair_findings = _compare_matched_pair(
            pair,
            target=target,
            scope=scope,
            sources_by_id=sources_by_id,
            assertions_by_id=assertions_by_id,
            policy=policy,
            workspace_secret=workspace_secret,
        )
        findings.extend(pair_findings)
        codes = {finding.code for finding in pair_findings}
        if codes & _STALE_CODES:
            stale_count += 1
        if pair.expected.acl_projection is not None and len(pair.expected.acl_projection) > 0:
            acl_required_count += 1
            if not (codes & _ACL_VIOLATION_CODES):
                acl_compliant_count += 1

    for expected in outcome.missing_expected:
        findings.append(_classify_missing(expected, target=target, scope=scope))

    unverifiable_count = 0
    for observed in outcome.orphan_observed:
        finding = _classify_orphan(
            observed, target=target, scope=scope, sources_by_id=sources_by_id
        )
        findings.append(finding)
        if finding.code == FindingCode.UNVERIFIABLE_POINT:
            unverifiable_count += 1

    findings.extend(_duplicate_findings(outcome, target=target, scope=scope))

    if manifest.build.status != "complete":
        findings.append(
            build_finding(
                code=FindingCode.MANIFEST_INCOMPLETE,
                target=target,
                scope=scope,
                subject_id=manifest.build.build_id,
                affected_field="build.status",
                evidence={"status": manifest.build.status},
            )
        )

    if consistency_completeness == SnapshotCompleteness.INCOMPLETE:
        findings.append(
            build_finding(
                code=FindingCode.SNAPSHOT_INCOMPLETE,
                target=target,
                scope=scope,
                subject_id=f"{target}:{scope}",
                affected_field="consistency.completeness",
                evidence={
                    "start_count": consistency_caveat.start_count,
                    "end_count": consistency_caveat.end_count,
                },
            )
        )

    findings.sort(key=lambda finding: finding.fingerprint)

    ratios = Ratios(
        lineage_coverage=_ratio(observed_count - unverifiable_count, observed_count),
        missing_ratio=_ratio(len(outcome.missing_expected), len(expected_points)),
        orphan_ratio=_ratio(len(outcome.orphan_observed), observed_count),
        stale_ratio=_ratio(stale_count, len(outcome.matched)),
        acl_compliance=_ratio(acl_compliant_count, acl_required_count),
    )
    summary = Summary(
        target=target,
        scope=scope,
        expected_bindings=len(expected_points),
        observed_points=observed_count,
        matched_points=len(outcome.matched),
        finding_count=len(findings),
        finding_count_by_severity=_severity_counts(findings),
        manifest_signed=len(manifest.signatures) > 0,
    )
    return ReconciliationResult(
        summary=summary,
        ratios=ratios,
        findings=findings,
        consistency=consistency_caveat,
        manifest_id=manifest.build.build_id,
        manifest_status=manifest.build.status,
    )


# --------------------------------------------------------------------------
# Matched-pair field comparison (section 14.3)
# --------------------------------------------------------------------------


def _compare_matched_pair(
    pair: MatchedPair,
    *,
    target: str,
    scope: str,
    sources_by_id: dict[str, SourceRecord],
    assertions_by_id: dict[str, Assertion],
    policy: PolicyDocument | None,
    workspace_secret: bytes | None,
) -> list[Finding]:
    findings: list[Finding] = []
    expected, observed = pair.expected, pair.observed
    subject = expected.binding_id
    point_id = observed.point_id
    lineage = AffectedLineage(
        source_id=expected.source_id,
        source_version_id=expected.source_version_id,
        chunk_id=expected.chunk_id,
        embedding_id=expected.embedding_id,
    )

    def _finding(
        code: FindingCode,
        affected_field: str,
        evidence: dict[str, Any],
        *,
        severity: FindingSeverity | None = None,
        severity_override_reason: str | None = None,
    ) -> Finding:
        return build_finding(
            code=code,
            target=target,
            scope=scope,
            subject_id=subject,
            affected_field=affected_field,
            severity=severity,
            severity_override_reason=severity_override_reason,
            point_id=point_id,
            binding_id=expected.binding_id,
            affected_lineage=lineage,
            evidence=evidence,
            match_level=int(pair.level),
            confidence=pair.confidence,
        )

    # --- source/parse/chunk staleness -------------------------------------------------
    stale = False
    if expected.source_id and expected.source_id in sources_by_id:
        current_source = sources_by_id[expected.source_id]
        if observed.source_version_id and observed.source_version_id != current_source.version_id:
            findings.append(
                _finding(
                    FindingCode.STALE_SOURCE,
                    "source_version_id",
                    {
                        "expected_source_version_id": current_source.version_id,
                        "observed_source_version_id": observed.source_version_id,
                    },
                )
            )
            stale = True
        elif expected.chunk_id and observed.chunk_id and observed.chunk_id != expected.chunk_id:
            findings.append(
                _finding(
                    FindingCode.STALE_CHUNKING,
                    "chunk_id",
                    {
                        "expected_chunk_id": expected.chunk_id,
                        "observed_chunk_id": observed.chunk_id,
                    },
                )
            )
            stale = True

    # --- embedding dimension / vector hash ---------------------------------------------
    if expected.embedding_dimension is not None and observed.vector_dimensions:
        observed_dims = set(observed.vector_dimensions.values())
        if expected.embedding_dimension not in observed_dims:
            findings.append(
                _finding(
                    FindingCode.EMBEDDING_DIMENSION_MISMATCH,
                    "dimension",
                    {
                        "expected_dimension": expected.embedding_dimension,
                        "observed_dimensions": sorted(observed_dims),
                    },
                )
            )

    if (
        expected.embedding_vector_hash
        and observed.vector_hashes
        and expected.embedding_vector_hash not in observed.vector_hashes.values()
    ):
        findings.append(
            _finding(
                FindingCode.VECTOR_HASH_MISMATCH,
                "vector_hash",
                {"expected_vector_hash": expected.embedding_vector_hash},
            )
        )

    # --- tenant --------------------------------------------------------------------------
    if expected.tenant_projection:
        if not observed.tenant:
            findings.append(
                _finding(
                    FindingCode.TENANT_MISSING,
                    "tenant",
                    {"expected_tenant": expected.tenant_projection},
                )
            )
        elif observed.tenant != expected.tenant_projection:
            findings.append(
                _finding(
                    FindingCode.TENANT_MISMATCH,
                    "tenant",
                    {
                        "expected_tenant": expected.tenant_projection,
                        "observed_tenant": observed.tenant,
                    },
                )
            )

    # --- ACL -----------------------------------------------------------------------------
    acl_finding_code: FindingCode | None = None
    if expected.acl_projection is not None:
        expected_acl = set(expected.acl_projection)
        observed_acl = set(observed.acl or [])
        if expected_acl and not observed_acl:
            acl_finding_code = FindingCode.ACL_MISSING
        elif observed_acl - expected_acl:
            acl_finding_code = FindingCode.ACL_BROADER_THAN_SOURCE
        elif observed_acl != expected_acl:
            acl_finding_code = FindingCode.ACL_MISMATCH
        if acl_finding_code is not None:
            findings.append(
                _finding(
                    acl_finding_code,
                    "acl",
                    {
                        "expected_acl": mask_acl_entries(expected_acl, workspace_secret),
                        "observed_acl": mask_acl_entries(observed_acl, workspace_secret),
                    },
                )
            )

    # --- payload drift catch-all ----------------------------------------------------------
    if (
        expected.expected_payload_hash != observed.payload_hash
        and not stale
        and acl_finding_code is None
        and not (expected.tenant_projection and observed.tenant != expected.tenant_projection)
    ):
        findings.append(
            _finding(
                FindingCode.PAYLOAD_DRIFT,
                "payload_hash",
                {
                    "expected_payload_hash": expected.expected_payload_hash,
                    "observed_payload_hash": observed.payload_hash,
                },
            )
        )

    # --- PII / license policy facts (section 14.3's last comparison) ---------------------
    if policy is not None:
        findings.extend(
            _pii_policy_findings(
                pair,
                assertions_by_id,
                policy.pii,
                target=target,
                scope=scope,
                subject=subject,
                point_id=point_id,
                lineage=lineage,
            )
        )
        findings.extend(
            _license_policy_findings(
                expected.license_assertion_ids,
                assertions_by_id,
                policy,
                target=target,
                scope=scope,
                subject=subject,
                point_id=point_id,
                lineage=lineage,
            )
        )

    return findings


def _resolve_pii_assertions(
    assertion_ids: Sequence[str], assertions_by_id: dict[str, Assertion]
) -> list[PiiScanAssertion]:
    return [
        assertion
        for aid in assertion_ids
        if isinstance((assertion := assertions_by_id.get(aid)), PiiScanAssertion)
    ]


def _pii_policy_findings(
    pair: MatchedPair,
    assertions_by_id: dict[str, Assertion],
    pii_policy: PiiPolicy | None,
    *,
    target: str,
    scope: str,
    subject: str,
    point_id: Any,
    lineage: AffectedLineage,
) -> list[Finding]:
    if pii_policy is None:
        return []
    findings: list[Finding] = []
    max_confidence_allowed = (
        pii_policy.max_confidence_allowed if pii_policy.max_confidence_allowed is not None else 1.0
    )
    for pii_assertion in _resolve_pii_assertions(pair.expected.pii_assertion_ids, assertions_by_id):
        for pii_finding in pii_assertion.findings:
            if not _pii_violates(pii_finding, pii_policy, max_confidence_allowed):
                continue
            severity = FindingSeverity.CRITICAL if pii_finding.confidence >= 0.9 else None
            findings.append(
                build_finding(
                    code=FindingCode.PII_POLICY_VIOLATION,
                    target=target,
                    scope=scope,
                    subject_id=subject,
                    affected_field=f"pii:{pii_finding.entity_type}:{pii_finding.start}",
                    severity=severity,
                    point_id=point_id,
                    binding_id=pair.expected.binding_id,
                    affected_lineage=lineage,
                    evidence={
                        "entity_type": pii_finding.entity_type,
                        "confidence": pii_finding.confidence,
                        "masked_preview": pii_finding.masked_preview,
                    },
                    match_level=int(pair.level),
                    confidence=pair.confidence,
                )
            )
    return findings


def _pii_violates(
    finding: PiiFinding, pii_policy: PiiPolicy, max_confidence_allowed: float
) -> bool:
    type_denied = finding.entity_type in pii_policy.deny
    type_not_allowlisted = bool(pii_policy.allow) and finding.entity_type not in pii_policy.allow
    return (type_denied or type_not_allowlisted) and finding.confidence > max_confidence_allowed


def _license_policy_findings(
    license_assertion_ids: Sequence[str],
    assertions_by_id: dict[str, Assertion],
    policy: PolicyDocument,
    *,
    target: str,
    scope: str,
    subject: str,
    point_id: Any,
    lineage: AffectedLineage,
) -> list[Finding]:
    if policy.licenses is None:
        return []
    candidates = [
        assertion
        for aid in license_assertion_ids
        if isinstance((assertion := assertions_by_id.get(aid)), LicenseAssertion)
    ]
    if not candidates:
        return []
    effective = min(candidates, key=lambda assertion: _LICENSE_PRECEDENCE.get(assertion.method, 99))
    expression = effective.spdx_expression
    if expression == "NOASSERTION":
        mode = policy.licenses.unknown or "allow"
        if mode == "allow":
            return []
        return [
            build_finding(
                code=FindingCode.LICENSE_UNKNOWN,
                target=target,
                scope=scope,
                subject_id=subject,
                affected_field="license",
                severity=FindingSeverity.HIGH if mode == "fail" else None,
                point_id=point_id,
                binding_id=None,
                affected_lineage=lineage,
                evidence={"method": effective.method},
            )
        ]
    deny = set(policy.licenses.deny)
    allow = set(policy.licenses.allow)
    if expression in deny or (allow and expression not in allow):
        return [
            build_finding(
                code=FindingCode.LICENSE_POLICY_VIOLATION,
                target=target,
                scope=scope,
                subject_id=subject,
                affected_field="license",
                point_id=point_id,
                binding_id=None,
                affected_lineage=lineage,
                evidence={"spdx_expression": expression, "method": effective.method},
            )
        ]
    return []


# --------------------------------------------------------------------------
# Missing / orphan classification
# --------------------------------------------------------------------------


def _classify_missing(expected: ExpectedPoint, *, target: str, scope: str) -> Finding:
    return build_finding(
        code=FindingCode.MISSING_IN_INDEX,
        target=target,
        scope=scope,
        subject_id=expected.binding_id,
        affected_field="point_id",
        point_id=expected.point_id,
        binding_id=expected.binding_id,
        affected_lineage=AffectedLineage(
            source_id=expected.source_id,
            source_version_id=expected.source_version_id,
            chunk_id=expected.chunk_id,
            embedding_id=expected.embedding_id,
        ),
        evidence={"write_status": expected.write_status} if expected.write_status else {},
    )


def _classify_orphan(
    observed: NormalizedPoint, *, target: str, scope: str, sources_by_id: dict[str, SourceRecord]
) -> Finding:
    subject = normalize_point_id(observed.point_id)
    lineage = AffectedLineage(
        source_id=observed.source_id,
        source_version_id=observed.source_version_id,
        chunk_id=observed.chunk_id,
        embedding_id=observed.embedding_id,
    )
    has_any_identity = any(
        (observed.embedding_id, observed.chunk_id, observed.source_id, observed.source_version_id)
    )

    def _finding(code: FindingCode, affected_field: str, evidence: dict[str, Any]) -> Finding:
        return build_finding(
            code=code,
            target=target,
            scope=scope,
            subject_id=subject,
            affected_field=affected_field,
            point_id=observed.point_id,
            affected_lineage=lineage,
            evidence=evidence,
        )

    if not has_any_identity:
        return _finding(FindingCode.UNVERIFIABLE_POINT, "identity", {})

    if observed.source_id:
        current = sources_by_id.get(observed.source_id)
        if current is not None:
            if observed.source_version_id and observed.source_version_id != current.version_id:
                return _finding(
                    FindingCode.STALE_SOURCE,
                    "source_version_id",
                    {
                        "expected_source_version_id": current.version_id,
                        "observed_source_version_id": observed.source_version_id,
                    },
                )
            return _finding(
                FindingCode.STALE_CHUNKING,
                "chunk_id",
                {
                    "observed_chunk_id": observed.chunk_id,
                    "observed_embedding_id": observed.embedding_id,
                },
            )
        return _finding(FindingCode.ORPHAN_IN_INDEX, "source_id", {"tombstone_hint": True})

    if observed.chunk_id or observed.embedding_id:
        return _finding(FindingCode.SOURCE_METADATA_MISSING, "source_id", {})

    return _finding(FindingCode.ORPHAN_IN_INDEX, "point_id", {})


def _duplicate_findings(outcome: MatchOutcome, *, target: str, scope: str) -> list[Finding]:
    findings: list[Finding] = []
    for level, key, group in outcome.duplicate_observed_groups:
        code = (
            FindingCode.DUPLICATE_POINT_ID
            if level is MatchLevel.POINT_ID
            else FindingCode.DUPLICATE_CONTENT
        )
        point_ids = sorted(normalize_point_id(item.point_id) for item in group)
        findings.append(
            build_finding(
                code=code,
                target=target,
                scope=scope,
                subject_id=key,
                affected_field=level.name.lower(),
                evidence={"duplicate_point_ids": point_ids, "count": len(group)},
            )
        )
    return findings
