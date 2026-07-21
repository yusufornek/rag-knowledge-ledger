"""Reconciliation matching order, per PROJECT_SPEC.md sections 9.1 and 14.2/14.3.

Section 9.1's matching order, exactly as listed there:

1. Exact expected point id.
2. Exact embedding id (as carried in the observed payload mapping).
3. Exact chunk id.
4. Source version (+ locator; see `resolve_expected_points`'s docstring
   for why this module folds "locator" out of level 4 -- `NormalizedPoint`
   has no locator field to compare against).
5. Content/payload hash heuristic.

Levels 1-3 are high-confidence and *close* a missing/orphan pair (a match at
any of these levels removes both sides from further consideration). Levels
4-5 are medium/low confidence and, per section 9.1's own text ("low-confidence
match missing/orphan'ı otomatik kapatmaz, suggestion üretir"), never close a
missing/orphan finding -- they only attach relocation-candidate suggestions
to findings that levels 1-3 already left unresolved.

`stream_merge_join` is the single sort-merge-join primitive every matching
round in this codebase uses, whether the two input streams are small
in-memory `sorted()` lists (`match_all_levels`, the small-data path in
`ragledger.reconcile.engine`) or `heapq.merge` generators reading several
external sorted run files back off disk (the big-data path). Using the exact
same function for both is what makes those two paths produce identical
matches for the same input -- see
`tests/reconcile/test_engine_equivalence.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from ragledger.connectors.base import NormalizedPoint
from ragledger.core.canonical import canonical_bytes
from ragledger.core.models import EmbeddingModelInfo, ManifestEnvelope, PointId

__all__ = [
    "CLOSING_LEVELS",
    "SUGGESTION_LEVELS",
    "ExpectedPoint",
    "MatchLevel",
    "MatchOutcome",
    "MatchedPair",
    "confidence_for_level",
    "expected_key",
    "expected_point_from_json_bytes",
    "expected_point_to_json_bytes",
    "expected_sort_key",
    "match_all_levels",
    "normalize_point_id",
    "observed_key",
    "observed_sort_key",
    "relocation_suggestions",
    "resolve_expected_points",
    "stream_merge_join",
]


class MatchLevel(IntEnum):
    """Section 9.1's five matching levels, in precedence order."""

    POINT_ID = 1
    EMBEDDING_ID = 2
    CHUNK_ID = 3
    SOURCE_VERSION = 4
    CONTENT_HASH = 5


CLOSING_LEVELS: tuple[MatchLevel, ...] = (
    MatchLevel.POINT_ID,
    MatchLevel.EMBEDDING_ID,
    MatchLevel.CHUNK_ID,
)
"""High-confidence levels (section 9.1: "Yalnız 1-3 high-confidence match")."""

SUGGESTION_LEVELS: tuple[MatchLevel, ...] = (
    MatchLevel.SOURCE_VERSION,
    MatchLevel.CONTENT_HASH,
)
"""Medium/low-confidence levels that only ever produce suggestions."""

_NEXT_CLOSING_LEVEL: dict[MatchLevel, MatchLevel] = {
    MatchLevel.POINT_ID: MatchLevel.EMBEDDING_ID,
    MatchLevel.EMBEDDING_ID: MatchLevel.CHUNK_ID,
}


def confidence_for_level(level: MatchLevel) -> str:
    """Return "high"/"medium"/"low" for a matching level, per section 9.1."""
    if level in CLOSING_LEVELS:
        return "high"
    if level is MatchLevel.SOURCE_VERSION:
        return "medium"
    return "low"


def normalize_point_id(point_id: PointId) -> str:
    """Canonical-JSON string key for a point id of any typed shape (FR-104/FR-115).

    Mirrors `ragledger.connectors.ndjson._point_id_key`'s convention
    independently, so a string, an integer, and a composite-key JSON object
    all compare and sort the same deterministic way here as they do in the
    snapshot format.
    """
    return canonical_bytes(point_id).decode("utf-8")


# --------------------------------------------------------------------------
# Expected side: manifest index bindings joined with their lineage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedPoint:
    """One manifest-derived expected index point: an `IndexBinding` joined
    with its embedding/chunk/source lineage -- section 9's "expected" side.

    `acl_projection`/`tenant_projection` follow the same "`None` means not
    declared, distinct from an explicit empty set" convention
    `ragledger.governance.acl.expected_acl_entries` uses: a binding that
    never declared an ACL/tenant expectation is not checked at all, while an
    explicit empty ACL (`()`) is an explicit "no access" expectation that IS
    checked (an observed point carrying any ACL entry at all would then be
    `ACL_BROADER_THAN_SOURCE`).
    """

    binding_id: str
    target: str
    scope: str
    point_id: PointId
    embedding_id: str
    embedding_model: EmbeddingModelInfo | None
    embedding_dimension: int | None
    embedding_vector_hash: str | None
    chunk_id: str | None
    parse_run_id: str | None
    parser_name: str | None
    parser_version: str | None
    parser_config_hash: str | None
    source_id: str | None
    source_version_id: str | None
    source_status: str | None
    expected_payload_hash: str
    expected_payload_projection: Mapping[str, Any] | None
    tenant_projection: str | None
    acl_projection: tuple[str, ...] | None
    write_status: str | None
    pii_assertion_ids: tuple[str, ...] = ()
    license_assertion_ids: tuple[str, ...] = ()

    @property
    def point_key(self) -> str:
        return normalize_point_id(self.point_id)


def _coerce_acl_projection(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return (str(value),)


def _coerce_tenant_projection(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and "value" in value:
        return str(value["value"])
    return str(value)


def resolve_expected_points(
    manifest: ManifestEnvelope, *, target: str, scope: str
) -> list[ExpectedPoint]:
    """Join `manifest.index_bindings` (filtered to one target/scope) with their
    embedding/chunk/source lineage into `ExpectedPoint`s.

    `target`/`scope` match `IndexBinding.target`/`IndexBinding.namespace`,
    which reconciliation's caller is responsible for knowing correspond to
    one connector's `NormalizedPoint.target_id`/`.scope` (FR-120's "expected
    manifest and observed snapshot compatible target scope check" -- that
    compatibility check itself is a caller/CLI-wiring concern, out of this
    package's scope).

    Deliberate scope note: section 9.1 level 4 is "source version + locator",
    but `NormalizedPoint` never carries a structural locator (only the six
    logical payload fields `ragledger.connectors.base._PROJECTABLE_FIELDS`
    lists: source_id, source_version_id, chunk_id, embedding_id, tenant,
    acl). Level 4 in this module therefore degrades to a source-version-only
    heuristic; see `docs/reviews/m6-status-notes.md` for the full writeup.
    """
    embeddings_by_id = {embedding.id: embedding for embedding in manifest.embeddings}
    chunks_by_id = {chunk.id: chunk for chunk in manifest.chunks}
    parse_runs_by_id = {parse_run.id: parse_run for parse_run in manifest.parse_runs}
    sources_by_version = {source.version_id: source for source in manifest.sources}

    resolved: list[ExpectedPoint] = []
    for binding in manifest.index_bindings:
        if binding.target != target or binding.namespace != scope:
            continue
        embedding = embeddings_by_id.get(binding.embedding_id)
        chunk = chunks_by_id.get(embedding.chunk_id) if embedding else None
        parse_run = parse_runs_by_id.get(chunk.parse_run_id) if chunk else None
        source = sources_by_version.get(chunk.source_version_id) if chunk else None
        resolved.append(
            ExpectedPoint(
                binding_id=binding.id,
                target=binding.target,
                scope=binding.namespace,
                point_id=binding.point_id,
                embedding_id=binding.embedding_id,
                embedding_model=embedding.model if embedding else None,
                embedding_dimension=embedding.dimension if embedding else None,
                embedding_vector_hash=embedding.vector_hash if embedding else None,
                chunk_id=chunk.id if chunk else None,
                parse_run_id=parse_run.id if parse_run else None,
                parser_name=parse_run.parser_name if parse_run else None,
                parser_version=parse_run.parser_version if parse_run else None,
                parser_config_hash=parse_run.config_hash if parse_run else None,
                source_id=source.id if source else None,
                source_version_id=source.version_id if source else None,
                source_status=source.status if source else None,
                expected_payload_hash=binding.expected_payload_hash,
                expected_payload_projection=binding.expected_payload_projection,
                tenant_projection=_coerce_tenant_projection(binding.tenant_projection),
                acl_projection=_coerce_acl_projection(binding.acl_projection),
                write_status=binding.write_status,
                pii_assertion_ids=tuple(chunk.pii_assertion_ids) if chunk else (),
                license_assertion_ids=tuple(chunk.license_assertion_ids) if chunk else (),
            )
        )
    return resolved


# --------------------------------------------------------------------------
# Level key functions
# --------------------------------------------------------------------------


def expected_key(level: MatchLevel, item: ExpectedPoint) -> str | None:
    if level is MatchLevel.POINT_ID:
        return item.point_key
    if level is MatchLevel.EMBEDDING_ID:
        return item.embedding_id
    if level is MatchLevel.CHUNK_ID:
        return item.chunk_id
    if level is MatchLevel.SOURCE_VERSION:
        return item.source_version_id
    if level is MatchLevel.CONTENT_HASH:
        return item.expected_payload_hash
    raise ValueError(f"unknown match level: {level!r}")


def observed_key(level: MatchLevel, item: NormalizedPoint) -> str | None:
    if level is MatchLevel.POINT_ID:
        return normalize_point_id(item.point_id)
    if level is MatchLevel.EMBEDDING_ID:
        return item.embedding_id
    if level is MatchLevel.CHUNK_ID:
        return item.chunk_id
    if level is MatchLevel.SOURCE_VERSION:
        return item.source_version_id
    if level is MatchLevel.CONTENT_HASH:
        return item.payload_hash
    raise ValueError(f"unknown match level: {level!r}")


def expected_sort_key(level: MatchLevel) -> Callable[[ExpectedPoint], tuple[bool, str]]:
    """A `None`-keys-sort-last key function for one level, over `ExpectedPoint`s.

    Used both for in-memory `sorted()` (small-data path) and as the `key=`
    passed to `heapq.merge` when reading several external sorted runs back
    (big-data path) -- both must rank `None` keys identically for the merge
    to line up.
    """

    def _key(item: ExpectedPoint) -> tuple[bool, str]:
        value = expected_key(level, item)
        return (value is None, value or "")

    return _key


def observed_sort_key(level: MatchLevel) -> Callable[[NormalizedPoint], tuple[bool, str]]:
    def _key(item: NormalizedPoint) -> tuple[bool, str]:
        value = observed_key(level, item)
        return (value is None, value or "")

    return _key


# --------------------------------------------------------------------------
# Matched pairs and outcomes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchedPair:
    expected: ExpectedPoint
    observed: NormalizedPoint
    level: MatchLevel
    confidence: str


DuplicateGroup = tuple[MatchLevel, str, list[Any]]


@dataclass(frozen=True)
class MatchOutcome:
    matched: list[MatchedPair]
    missing_expected: list[ExpectedPoint]
    orphan_observed: list[NormalizedPoint]
    duplicate_expected_groups: list[tuple[MatchLevel, str, list[ExpectedPoint]]] = field(
        default_factory=list
    )
    duplicate_observed_groups: list[tuple[MatchLevel, str, list[NormalizedPoint]]] = field(
        default_factory=list
    )
    relocation_candidates: dict[str, list[str]] = field(default_factory=dict)
    """`ExpectedPoint.binding_id` -> candidate observed point-id keys (level 4/5)."""
    orphan_candidates: dict[str, list[str]] = field(default_factory=dict)
    """Observed point-id key -> candidate `ExpectedPoint.binding_id`s (level 4/5)."""


# --------------------------------------------------------------------------
# The shared sort-merge-join primitive
# --------------------------------------------------------------------------


def stream_merge_join(
    level: MatchLevel,
    expected_sorted: Iterable[ExpectedPoint],
    observed_sorted: Iterable[NormalizedPoint],
    *,
    on_matched: Callable[[MatchedPair], None],
    on_leftover_expected: Callable[[ExpectedPoint], None],
    on_leftover_observed: Callable[[NormalizedPoint], None],
    on_duplicate_expected: Callable[[MatchLevel, str, list[ExpectedPoint]], None] | None = None,
    on_duplicate_observed: Callable[[MatchLevel, str, list[NormalizedPoint]], None] | None = None,
) -> None:
    """Sort-merge-join one matching level over two key-sorted streams.

    Both inputs must already be sorted ascending by this level's key (see
    `expected_sort_key`/`observed_sort_key`), `None` keys last. This is the
    ONLY matching-round implementation in this codebase: `match_all_levels`
    (small-data, plain sorted lists) and `ragledger.reconcile.engine`'s
    big-data path (a `heapq.merge` of several external sorted run files)
    both call this exact function, which is what guarantees they produce
    identical matches for the same logical input -- see
    `tests/reconcile/test_engine_equivalence.py`.

    Items whose key is `None` at this level can never match at this level
    and go straight to their side's leftover callback. When two or more
    items on the SAME side share a key (a duplicate), they are paired
    positionally against the other side's same-key group (first-with-first);
    any surplus on either side becomes leftover, and a duplicate group
    (`len(group) > 1`) is reported via the `on_duplicate_*` callbacks.
    """
    confidence = confidence_for_level(level)
    e_iter: Iterator[ExpectedPoint] = iter(expected_sorted)
    o_iter: Iterator[NormalizedPoint] = iter(observed_sorted)
    e_item = next(e_iter, None)
    o_item = next(o_iter, None)

    while e_item is not None and o_item is not None:
        ek = expected_key(level, e_item)
        ok = observed_key(level, o_item)
        if ek is None:
            on_leftover_expected(e_item)
            e_item = next(e_iter, None)
            continue
        if ok is None:
            on_leftover_observed(o_item)
            o_item = next(o_iter, None)
            continue
        if ek < ok:
            on_leftover_expected(e_item)
            e_item = next(e_iter, None)
        elif ek > ok:
            on_leftover_observed(o_item)
            o_item = next(o_iter, None)
        else:
            e_group = [e_item]
            e_item = next(e_iter, None)
            while e_item is not None and expected_key(level, e_item) == ek:
                e_group.append(e_item)
                e_item = next(e_iter, None)
            o_group = [o_item]
            o_item = next(o_iter, None)
            while o_item is not None and observed_key(level, o_item) == ok:
                o_group.append(o_item)
                o_item = next(o_iter, None)

            pair_count = min(len(e_group), len(o_group))
            for index in range(pair_count):
                on_matched(
                    MatchedPair(
                        expected=e_group[index],
                        observed=o_group[index],
                        level=level,
                        confidence=confidence,
                    )
                )
            for extra_expected in e_group[pair_count:]:
                on_leftover_expected(extra_expected)
            for extra_observed in o_group[pair_count:]:
                on_leftover_observed(extra_observed)
            if len(e_group) > 1 and on_duplicate_expected is not None:
                on_duplicate_expected(level, ek, e_group)
            if len(o_group) > 1 and on_duplicate_observed is not None:
                on_duplicate_observed(level, ok, o_group)

    while e_item is not None:
        on_leftover_expected(e_item)
        e_item = next(e_iter, None)
    while o_item is not None:
        on_leftover_observed(o_item)
        o_item = next(o_iter, None)


# --------------------------------------------------------------------------
# Small-data convenience: full in-memory multi-level match
# --------------------------------------------------------------------------


def match_all_levels(
    expected: Sequence[ExpectedPoint], observed: Sequence[NormalizedPoint]
) -> MatchOutcome:
    """In-memory (small-data) multi-level matcher, and the reference
    implementation the big-data external-merge path is checked against for
    equivalence.
    """
    matched: list[MatchedPair] = []
    duplicate_expected: list[tuple[MatchLevel, str, list[ExpectedPoint]]] = []
    duplicate_observed: list[tuple[MatchLevel, str, list[NormalizedPoint]]] = []
    remaining_expected: list[ExpectedPoint] = list(expected)
    remaining_observed: list[NormalizedPoint] = list(observed)

    for level in CLOSING_LEVELS:
        sorted_expected = sorted(remaining_expected, key=expected_sort_key(level))
        sorted_observed = sorted(remaining_observed, key=observed_sort_key(level))
        next_expected: list[ExpectedPoint] = []
        next_observed: list[NormalizedPoint] = []
        stream_merge_join(
            level,
            sorted_expected,
            sorted_observed,
            on_matched=matched.append,
            on_leftover_expected=next_expected.append,
            on_leftover_observed=next_observed.append,
            on_duplicate_expected=lambda lvl, key, group: duplicate_expected.append(
                (lvl, key, group)
            ),
            on_duplicate_observed=lambda lvl, key, group: duplicate_observed.append(
                (lvl, key, group)
            ),
        )
        remaining_expected, remaining_observed = next_expected, next_observed

    relocation, orphan = relocation_suggestions(remaining_expected, remaining_observed)
    return MatchOutcome(
        matched=matched,
        missing_expected=remaining_expected,
        orphan_observed=remaining_observed,
        duplicate_expected_groups=duplicate_expected,
        duplicate_observed_groups=duplicate_observed,
        relocation_candidates=relocation,
        orphan_candidates=orphan,
    )


def relocation_suggestions(
    expected: Sequence[ExpectedPoint], observed: Sequence[NormalizedPoint]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Level 4/5 suggestion-only passes over the final, unresolved leftovers.

    Never removes an item from missing/orphan (section 9.1); only attaches
    candidate ids for remediation/report display. Both levels run
    independently over the SAME leftover pool (not chained), since section
    9.1 lists them as two separate heuristics, not a pipeline.
    """
    relocation: dict[str, list[str]] = {}
    orphan: dict[str, list[str]] = {}
    for level in SUGGESTION_LEVELS:
        sorted_expected = sorted(expected, key=expected_sort_key(level))
        sorted_observed = sorted(observed, key=observed_sort_key(level))

        def _on_matched(pair: MatchedPair) -> None:
            okey = normalize_point_id(pair.observed.point_id)
            relocation.setdefault(pair.expected.binding_id, []).append(okey)
            orphan.setdefault(okey, []).append(pair.expected.binding_id)

        stream_merge_join(
            level,
            sorted_expected,
            sorted_observed,
            on_matched=_on_matched,
            on_leftover_expected=lambda _item: None,
            on_leftover_observed=lambda _item: None,
        )
    return relocation, orphan


# --------------------------------------------------------------------------
# ExpectedPoint (de)serialization, for the big-data path's spilled run files
# --------------------------------------------------------------------------


def expected_point_to_dict(item: ExpectedPoint) -> dict[str, Any]:
    return {
        "binding_id": item.binding_id,
        "target": item.target,
        "scope": item.scope,
        "point_id": item.point_id,
        "embedding_id": item.embedding_id,
        "embedding_model": (
            item.embedding_model.model_dump(mode="json") if item.embedding_model else None
        ),
        "embedding_dimension": item.embedding_dimension,
        "embedding_vector_hash": item.embedding_vector_hash,
        "chunk_id": item.chunk_id,
        "parse_run_id": item.parse_run_id,
        "parser_name": item.parser_name,
        "parser_version": item.parser_version,
        "parser_config_hash": item.parser_config_hash,
        "source_id": item.source_id,
        "source_version_id": item.source_version_id,
        "source_status": item.source_status,
        "expected_payload_hash": item.expected_payload_hash,
        "expected_payload_projection": (
            dict(item.expected_payload_projection) if item.expected_payload_projection else None
        ),
        "tenant_projection": item.tenant_projection,
        "acl_projection": (list(item.acl_projection) if item.acl_projection is not None else None),
        "write_status": item.write_status,
        "pii_assertion_ids": list(item.pii_assertion_ids),
        "license_assertion_ids": list(item.license_assertion_ids),
    }


def expected_point_from_dict(data: Mapping[str, Any]) -> ExpectedPoint:
    model = data.get("embedding_model")
    acl_projection = data.get("acl_projection")
    return ExpectedPoint(
        binding_id=data["binding_id"],
        target=data["target"],
        scope=data["scope"],
        point_id=data["point_id"],
        embedding_id=data["embedding_id"],
        embedding_model=EmbeddingModelInfo.model_validate(model) if model else None,
        embedding_dimension=data.get("embedding_dimension"),
        embedding_vector_hash=data.get("embedding_vector_hash"),
        chunk_id=data.get("chunk_id"),
        parse_run_id=data.get("parse_run_id"),
        parser_name=data.get("parser_name"),
        parser_version=data.get("parser_version"),
        parser_config_hash=data.get("parser_config_hash"),
        source_id=data.get("source_id"),
        source_version_id=data.get("source_version_id"),
        source_status=data.get("source_status"),
        expected_payload_hash=data["expected_payload_hash"],
        expected_payload_projection=data.get("expected_payload_projection"),
        tenant_projection=data.get("tenant_projection"),
        acl_projection=(tuple(acl_projection) if acl_projection is not None else None),
        write_status=data.get("write_status"),
        pii_assertion_ids=tuple(data.get("pii_assertion_ids", ())),
        license_assertion_ids=tuple(data.get("license_assertion_ids", ())),
    )


def expected_point_to_json_bytes(item: ExpectedPoint) -> bytes:
    """Encode one `ExpectedPoint` to a spill-run-file line.

    Deliberately plain `json.dumps`, not `ragledger.core.canonical.canonical_bytes`:
    a spill run file is scratch data internal to one `reconcile_big_data`
    call (round-tripped by this same process, never hashed, never compared
    byte-for-byte, never persisted past that call), so RFC 8785's UTF-16
    key-ordering pass -- worthwhile for a manifest's cross-process,
    cross-language content identity -- is pure overhead here. Plain `json`
    is materially faster at the point-count scale this path exists for.
    """
    return json.dumps(expected_point_to_dict(item), separators=(",", ":")).encode("utf-8")


def expected_point_from_json_bytes(data: bytes) -> ExpectedPoint:
    return expected_point_from_dict(json.loads(data))
