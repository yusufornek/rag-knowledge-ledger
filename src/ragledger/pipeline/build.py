"""End-to-end pipeline orchestration: discover -> parse -> chunk -> scan -> embed -> manifest.

Per the design specification section 10's pipeline diagram and 10.2's failure
behavior. `build_pipeline` is the single entry point this release
ships: it wires `ragledger.pipeline.discovery`, `.parsers`, `.chunkers`,
`.embedding`, `.cache`, and `ragledger.governance.{pii,license,acl}`
together and calls `ragledger.core.manifest.build_manifest` to produce
a schema-valid `ManifestEnvelope`.

Determinism is the whole point: `created_at` and `build_id` are always
caller-supplied (never wall-clock or random), every adapter used here
is deterministic (the shipped parsers, chunkers, and
`DeterministicLocalEmbeddingProvider`), and every record list is sorted
by a stable key before being handed to `build_manifest`. With
`BuildConfig.reproducible=True` (the design specification section 7.2's
`--reproducible` mode), calling `build_pipeline` twice with the same
source tree and the same `BuildConfig` produces byte-identical
canonical manifest bytes (`ragledger.core.manifest.canonical_manifest_bytes`).
`reproducible=False` (the default) additionally records each parse
run's real wall-clock `duration_seconds` as honest telemetry -- which,
being real measured timing, is by definition not identical between two
runs of the same input, so byte-identical output is only guaranteed in
`reproducible` mode.

Scope notes (honest gaps, not silent ones):

- Index bindings (the "Bind" stage in section 10's diagram) are not
  produced here: expected point-id/target mapping depends on connector
  and target configuration, which is M5 scope and out of this wave's
  ownership (`src/ragledger/connectors/` belongs to a concurrent agent).
  `index_bindings` is always empty in a manifest this function returns.
- Governance-stage (PII/license/ACL) result caching is not implemented;
  only parse/chunk/embed are cached. These scans are cheap, bounded
  regex passes over already-in-memory text, unlike a parser subprocess,
  a chunking pass, or an embedding call.
- Per-chunk PII scanning covers contextualized chunk text only (the
  actual embedding input, and the most policy-relevant target); source
  level scanning covers the pre-chunking parsed text. The design specification
  section 40's edge case about `raw_chunk` vs `contextualized` scan
  targets being independently selectable is not fully implemented for
  the intermediate chunk-boundary-raw-text case.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ragledger.core import ids
from ragledger.core.artifacts import ArtifactStore
from ragledger.core.canonical import canonical_bytes
from ragledger.core.hashing import hash_canonical, hash_text
from ragledger.core.manifest import build_manifest
from ragledger.core.models import (
    ArtifactRef,
    Assertion,
    BuildEnvironment,
    BuildRecord,
    BuildStatus,
    ChunkRecord,
    EmbeddingModelInfo,
    EmbeddingRecord,
    ManifestEnvelope,
    Normalization,
    ParseRecord,
    PiiScanAssertion,
    QualityAssertion,
    Sensitivity,
    SourceRecord,
    StageRecord,
    WarningRecord,
)
from ragledger.core.models import Tokenizer as ManifestTokenizer
from ragledger.core.signing import sign_manifest
from ragledger.governance.acl import (
    AclConfig,
    TenantConfig,
    build_acl_assertion,
    build_tenant_assertion,
    expected_acl_entries,
    expected_tenant,
)
from ragledger.governance.license import LicenseConfig, detect_spdx_header, evaluate_license
from ragledger.governance.pii import PiiScanConfig, build_pii_scan_assertion
from ragledger.pipeline.cache import StageCache, stage_cache_key
from ragledger.pipeline.chunkers.base import (
    ChunkCandidate,
    Chunker,
    ChunkerRegistry,
    WhitespaceTokenizer,
    drop_empty_candidates,
)
from ragledger.pipeline.chunkers.base import (
    default_registry as default_chunker_registry,
)
from ragledger.pipeline.discovery import DiscoveryConfig, compute_tombstones, discover_sources
from ragledger.pipeline.embedding import (
    EmbeddingProvider,
    EmbeddingProviderError,
    validate_dimension,
    validate_finite,
)
from ragledger.pipeline.embedding import vector_hash as compute_vector_hash
from ragledger.pipeline.parsers.base import (
    DocumentParser,
    LedgerDocument,
    ParseLimits,
    ParseOutcome,
    ParserRegistry,
)
from ragledger.pipeline.parsers.base import default_registry as default_parser_registry
from ragledger.pipeline.parsers.sandbox import run_sandboxed

_NO_PARSER_NAME = "none"
_NO_PARSER_VERSION = "0"
_DISCOVERY_STAGE_NAME = "ragledger.discovery"
_DISCOVERY_STAGE_VERSION = "1"
_PARSED_DOCUMENT_MEDIA_TYPE = "application/vnd.ragledger.ledger-document+json"


@dataclass(frozen=True)
class PiiPolicyConfig:
    """A minimal, build-time-only PII policy: which entity types block embedding.

    Full policy verdicts (PASS/WARN/FAIL/INCONCLUSIVE) are M6 scope;
    this only implements the design specification section 10.2's stated build
    time behavior directly: "PII/license policy block: embedding/index
    binding default skip."
    """

    block_entity_types: frozenset[str] = field(default_factory=frozenset)
    min_confidence: float = 0.0


@dataclass(frozen=True)
class BuildConfig:
    namespace: str
    root: Path
    build_id: str
    created_at: datetime
    ledger_version: str = "0.1.0"
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    parser_registry: ParserRegistry = field(default_factory=default_parser_registry)
    parser_config: dict[str, Any] = field(default_factory=dict)
    parse_limits: ParseLimits = field(default_factory=ParseLimits)
    sandboxed: bool = True
    chunker_registry: ChunkerRegistry = field(default_factory=default_chunker_registry)
    chunker_name: str = "hierarchical"
    chunker_config: dict[str, Any] = field(default_factory=dict)
    embedding_provider: EmbeddingProvider | None = None
    embedding_normalization: Normalization = "l2"
    pii_config: PiiScanConfig | None = None
    pii_policy: PiiPolicyConfig = field(default_factory=PiiPolicyConfig)
    license_config: LicenseConfig | None = None
    acl_config: AclConfig | None = None
    tenant_config: TenantConfig | None = None
    previous_sources: tuple[SourceRecord, ...] = ()
    signing_key: Ed25519PrivateKey | None = None
    signing_issuer: str | None = None
    reproducible: bool = False
    """When true, `ParseRecord.duration_seconds` is fixed to `0.0` instead
    of the real measured parse duration (the design specification section 7.2's
    `--reproducible` mode). Real wall-clock duration is legitimate,
    non-fabricated telemetry -- but by definition it varies from run to
    run on the same input, which is fundamentally incompatible with
    FR-082's "same input/config/reproducible epoch produces a
    byte-identical canonical manifest". Production builds should leave
    this `False` to keep real timing in the manifest; reproducible/test
    builds set it `True` deliberately, an explicit, visible trade-off
    rather than a silently nondeterministic default.
    """


def _uri_to_relative_path(uri: str) -> str:
    scheme, _, relative = uri.partition(":")
    if scheme != "file":
        raise ValueError(f"unsupported source URI scheme: {uri!r}")
    return relative


def _serialize_candidate(candidate: ChunkCandidate) -> dict[str, Any]:
    return {
        "element_refs": list(candidate.element_refs),
        "raw_text": candidate.raw_text,
        "locator": candidate.locator.model_dump(mode="json", exclude_none=True),
        "heading_path": candidate.heading_path,
        "table_caption": candidate.table_caption,
    }


def _deserialize_candidate(data: dict[str, Any]) -> ChunkCandidate:
    from ragledger.core.models import StructuralLocator

    return ChunkCandidate(
        element_refs=tuple(data["element_refs"]),
        raw_text=data["raw_text"],
        locator=StructuralLocator.model_validate(data["locator"]),
        heading_path=data["heading_path"],
        table_caption=data["table_caption"],
    )


def _run_parser(
    parser: DocumentParser,
    data: bytes,
    config: dict[str, Any],
    limits: ParseLimits,
    sandboxed: bool,
) -> ParseOutcome:
    if sandboxed:
        return run_sandboxed(parser, data, config, limits)
    return parser.parse(data, config, limits)


def _no_parser_outcome(consumed_hash: str) -> ParseOutcome:
    return ParseOutcome(
        status="fail",
        consumed_input_hash=consumed_hash,
        errors=["NO_PARSER_AVAILABLE"],
        duration_seconds=0.0,
    )


def _store_json_artifact(
    store: ArtifactStore, value: Any, media_type: str, sensitivity: Sensitivity
) -> ArtifactRef:
    payload = canonical_bytes(value)
    info = store.put(payload)
    return ArtifactRef(
        artifact_id=f"art_{info.sha256}",
        media_type=media_type,
        sha256=info.sha256,
        size_bytes=info.size_bytes,
        compression="none",
        encryption="none",
        locator=f"artifacts/{info.sha256}",
        sensitivity=sensitivity,
    )


@dataclass
class _BuildState:
    sources: list[SourceRecord] = field(default_factory=list)
    parse_runs: list[ParseRecord] = field(default_factory=list)
    chunks: list[ChunkRecord] = field(default_factory=list)
    embeddings: list[EmbeddingRecord] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    warnings: list[WarningRecord] = field(default_factory=list)
    any_parse_failures: bool = False


def _process_source(
    config: BuildConfig,
    source: SourceRecord,
    artifact_store: ArtifactStore,
    cache: StageCache,
    state: _BuildState,
) -> None:
    relative_path = _uri_to_relative_path(source.uri)
    absolute_path = Path(config.root) / relative_path
    raw_bytes = absolute_path.read_bytes()

    raw_info = artifact_store.put(raw_bytes)
    raw_ref = ArtifactRef(
        artifact_id=f"art_{raw_info.sha256}",
        media_type=source.media_type,
        sha256=raw_info.sha256,
        size_bytes=raw_info.size_bytes,
        compression="none",
        encryption="none",
        locator=f"artifacts/{raw_info.sha256}",
        sensitivity="restricted",
    )
    state.artifacts.append(raw_ref)
    source = source.model_copy(update={"raw_artifact_ref": raw_ref.artifact_id})

    parser = config.parser_registry.get(source.media_type)
    if parser is None:
        parser_config_hash = hash_canonical({"parser": _NO_PARSER_NAME})
        parse_run_id = ids.parse_run_id(source.version_id, parser_config_hash)
        outcome = _no_parser_outcome(source.content_hash)
        parsed_artifact_ref = _store_json_artifact(
            artifact_store,
            outcome.model_dump(mode="json", exclude_none=True),
            "application/json",
            "internal",
        )
        state.artifacts.append(parsed_artifact_ref)
        state.parse_runs.append(
            ParseRecord(
                id=parse_run_id,
                source_version_id=source.version_id,
                parser_name=_NO_PARSER_NAME,
                parser_version=_NO_PARSER_VERSION,
                config_hash=parser_config_hash,
                status="fail",
                warnings=[
                    WarningRecord(
                        code="NO_PARSER_AVAILABLE",
                        message=f"no parser registered for media type {source.media_type!r}",
                    )
                ],
                parsed_artifact_ref=parsed_artifact_ref.artifact_id,
                duration_seconds=0.0,
            )
        )
        state.any_parse_failures = True
        state.sources.append(source)
        return

    descriptor = parser.descriptor()
    parser_config_hash = hash_canonical(
        {"parser": descriptor.name, "version": descriptor.version, "config": config.parser_config}
    )
    parse_run_id = ids.parse_run_id(source.version_id, parser_config_hash)
    cache_key = stage_cache_key(
        "parse", source.content_hash, descriptor.name, descriptor.version, parser_config_hash
    )
    cached = cache.get(cache_key)
    if cached is not None:
        outcome = ParseOutcome.model_validate(cached)
    else:
        outcome = _run_parser(
            parser, raw_bytes, config.parser_config, config.parse_limits, config.sandboxed
        )
        cache.put(cache_key, outcome.model_dump(mode="json", exclude_none=True))

    parsed_payload: dict[str, Any] = (
        outcome.document.model_dump(mode="json", exclude_none=True)
        if outcome.document is not None
        else {"status": outcome.status, "errors": outcome.errors}
    )
    parsed_artifact_ref = _store_json_artifact(
        artifact_store, parsed_payload, _PARSED_DOCUMENT_MEDIA_TYPE, "internal"
    )
    state.artifacts.append(parsed_artifact_ref)

    state.parse_runs.append(
        ParseRecord(
            id=parse_run_id,
            source_version_id=source.version_id,
            parser_name=descriptor.name,
            parser_version=descriptor.version,
            config_hash=parser_config_hash,
            status=outcome.status,
            warnings=outcome.warnings,
            ocr=outcome.ocr,
            parsed_artifact_ref=parsed_artifact_ref.artifact_id,
            duration_seconds=0.0 if config.reproducible else outcome.duration_seconds,
        )
    )
    if outcome.status == "fail":
        state.any_parse_failures = True

    # -- Governance: license/ACL/tenant are source-level; PII source-level scan too --
    frontmatter = outcome.document.frontmatter if outcome.document else None
    spdx_header = (
        detect_spdx_header(raw_bytes.decode("utf-8", errors="replace"))
        if outcome.document
        else None
    )
    acl_assertion_id: str | None = None
    if config.acl_config is not None:
        entries = expected_acl_entries(relative_path, config.acl_config)
        if entries is not None:
            acl_assertion = build_acl_assertion(
                source.version_id,
                entries,
                config.created_at,
                case_normalize=config.acl_config.case_normalize,
            )
            state.assertions.append(acl_assertion)
            acl_assertion_id = acl_assertion.id

    declared_tenant_value: str | None = None
    if config.tenant_config is not None:
        tenant = expected_tenant(relative_path, config.tenant_config)
        if tenant is not None:
            tenant_key, tenant_value = tenant
            tenant_assertion = build_tenant_assertion(
                source.version_id, tenant_key, tenant_value, config.created_at
            )
            state.assertions.append(tenant_assertion)
            declared_tenant_value = tenant_value
        elif config.tenant_config.required:
            state.warnings.append(
                WarningRecord(code="TENANT_REQUIRED_BUT_MISSING", context={"uri": source.uri})
            )

    license_assertion_ids: list[str] = []
    if config.license_config is not None:
        effective, all_candidates = evaluate_license(
            relative_path,
            frontmatter,
            None,
            config.license_config,
            source.version_id,
            config.created_at,
            spdx_header,
        )
        state.assertions.extend(all_candidates)
        license_assertion_ids = [effective.id]

    if config.pii_config is not None and outcome.document is not None:
        source_text = "\n\n".join(element.text for element in outcome.document.elements)
        source_pii = build_pii_scan_assertion(
            source.version_id, source_text, config.pii_config, config.created_at
        )
        state.assertions.append(source_pii)

    source = source.model_copy(
        update={
            "declared_acl_assertion_id": acl_assertion_id,
            "declared_tenant": declared_tenant_value,
            "license_assertion_ids": license_assertion_ids,
        }
    )
    state.sources.append(source)

    if outcome.document is None or not outcome.document.elements:
        return

    _process_chunks(config, source, parse_run_id, outcome.document, cache, acl_assertion_id, state)


def _process_chunks(
    config: BuildConfig,
    source: SourceRecord,
    parse_run_id: str,
    document: LedgerDocument,
    cache: StageCache,
    acl_assertion_id: str | None,
    state: _BuildState,
) -> None:
    chunker: Chunker | None = config.chunker_registry.get(config.chunker_name)
    if chunker is None:
        raise ValueError(f"unknown chunker: {config.chunker_name!r}")
    chunker.validate_config(config.chunker_config)
    chunker_descriptor = chunker.descriptor()
    chunker_config_hash = hash_canonical(
        {
            "chunker": chunker_descriptor.name,
            "version": chunker_descriptor.version,
            "config": config.chunker_config,
        }
    )
    parsed_document_hash = hash_canonical(document.model_dump(mode="json", exclude_none=True))
    cache_key = stage_cache_key(
        "chunk",
        parsed_document_hash,
        chunker_descriptor.name,
        chunker_descriptor.version,
        chunker_config_hash,
    )
    cached = cache.get(cache_key)
    dropped_empty = 0
    if cached is not None:
        candidates = [_deserialize_candidate(item) for item in cached["candidates"]]
        dropped_empty = cached["dropped_empty"]
    else:
        candidates, dropped_empty = drop_empty_candidates(
            chunker.iterate_chunks(document, config.chunker_config)
        )
        cache.put(
            cache_key,
            {
                "candidates": [_serialize_candidate(candidate) for candidate in candidates],
                "dropped_empty": dropped_empty,
            },
        )
    if dropped_empty:
        state.assertions.append(
            QualityAssertion(
                id=_quality_assertion_id(parse_run_id, "EMPTY_CHUNKS_DROPPED"),
                subject_ref=parse_run_id,
                created_at=config.created_at,
                warnings=[
                    WarningRecord(code="EMPTY_CHUNKS_DROPPED", context={"count": dropped_empty})
                ],
            )
        )

    chunk_records: list[ChunkRecord] = []
    raw_hash_seen: dict[str, list[str]] = {}
    for candidate in candidates:
        contextualized = chunker.contextualize(candidate, config.chunker_config)
        raw_hash = hash_text(candidate.raw_text)
        contextualized_hash = hash_text(contextualized.contextualized_text)
        locator_json = candidate.locator.model_dump(mode="json", exclude_none=True)
        chunk_id_value = ids.chunk_id(parse_run_id, chunker_config_hash, locator_json, raw_hash)

        pii_assertion_ids: list[str] = []
        if config.pii_config is not None:
            pii_assertion = build_pii_scan_assertion(
                chunk_id_value,
                contextualized.contextualized_text,
                config.pii_config,
                config.created_at,
            )
            state.assertions.append(pii_assertion)
            pii_assertion_ids = [pii_assertion.id]

        chunk_record = ChunkRecord(
            id=chunk_id_value,
            source_version_id=source.version_id,
            parse_run_id=parse_run_id,
            locator=candidate.locator,
            raw_hash=raw_hash,
            contextualized_hash=contextualized_hash,
            token_count=contextualized.token_count,
            tokenizer=ManifestTokenizer(
                name=WhitespaceTokenizer.NAME, revision=WhitespaceTokenizer.REVISION
            ),
            pii_assertion_ids=pii_assertion_ids,
            acl_assertion_ids=[acl_assertion_id] if acl_assertion_id else [],
        )
        chunk_records.append(chunk_record)
        raw_hash_seen.setdefault(raw_hash, []).append(chunk_id_value)

        blocked = _pii_blocked(state.assertions, pii_assertion_ids, config.pii_policy)
        if config.embedding_provider is not None and not blocked:
            _embed_chunk(config, chunk_record, contextualized.contextualized_text, cache, state)

    duplicates = {
        raw_hash: chunk_ids for raw_hash, chunk_ids in raw_hash_seen.items() if len(chunk_ids) > 1
    }
    if duplicates:
        state.assertions.append(
            QualityAssertion(
                id=_quality_assertion_id(parse_run_id, "DUPLICATE_CHUNK_CONTENT"),
                subject_ref=parse_run_id,
                created_at=config.created_at,
                warnings=[
                    WarningRecord(
                        code="DUPLICATE_CHUNK_CONTENT",
                        context={
                            "chunk_ids": sorted(
                                chunk_id for ids_ in duplicates.values() for chunk_id in ids_
                            )
                        },
                    )
                ],
            )
        )

    state.chunks.extend(chunk_records)


def _pii_blocked(
    assertions: Sequence[Assertion], assertion_ids: list[str], policy: PiiPolicyConfig
) -> bool:
    if not assertion_ids or not policy.block_entity_types:
        return False
    by_id = {assertion.id: assertion for assertion in assertions}
    for assertion_id in assertion_ids:
        assertion = by_id.get(assertion_id)
        if not isinstance(assertion, PiiScanAssertion):
            continue
        for finding in assertion.findings:
            if (
                finding.entity_type in policy.block_entity_types
                and finding.confidence >= policy.min_confidence
            ):
                return True
    return False


def _quality_assertion_id(subject_ref: str, code: str) -> str:
    from ragledger.governance.identity import derive_assertion_id

    return derive_assertion_id("qua", subject_ref, code)


def _embed_chunk(
    config: BuildConfig,
    chunk: ChunkRecord,
    contextualized_text: str,
    cache: StageCache,
    state: _BuildState,
) -> None:
    provider = config.embedding_provider
    assert provider is not None
    descriptor = provider.descriptor()
    embedding_config_hash = hash_canonical(
        {
            "provider": descriptor.provider,
            "name": descriptor.name,
            "revision": descriptor.revision,
            "dimension": descriptor.dimension,
            "dtype": descriptor.dtype,
            "normalization": config.embedding_normalization,
        }
    )
    cache_key = stage_cache_key(
        "embed",
        chunk.contextualized_hash,
        f"{descriptor.provider}:{descriptor.name}",
        descriptor.revision,
        embedding_config_hash,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        vector = cached["vector"]
        usage = cached["usage"]
    else:
        vector, usage = _embed_with_retry(provider, contextualized_text)
        if vector is None:
            state.warnings.append(
                WarningRecord(code="EMBEDDING_FAILED", context={"chunk_id": chunk.id})
            )
            return
        validate_finite(vector)
        validate_dimension(descriptor, vector)
        cache.put(cache_key, {"vector": vector, "usage": usage})

    embedding_id_value = ids.embedding_id(
        chunk.id, chunk.contextualized_hash, embedding_config_hash
    )
    state.embeddings.append(
        EmbeddingRecord(
            id=embedding_id_value,
            chunk_id=chunk.id,
            model=EmbeddingModelInfo(
                provider=descriptor.provider, name=descriptor.name, revision=descriptor.revision
            ),
            dimension=descriptor.dimension,
            dtype=descriptor.dtype,
            normalization=config.embedding_normalization,
            contextualized_hash=chunk.contextualized_hash,
            vector_hash=compute_vector_hash(vector),
            generated_at=config.created_at,
            usage=usage,
        )
    )


def _embed_with_retry(
    provider: EmbeddingProvider, text: str
) -> tuple[list[float] | None, dict[str, Any]]:
    """Embed one text, retrying once on `EmbeddingProviderError` (section 10.2).

    Returns `(None, {})` if both attempts fail, so the caller can skip
    this chunk's embedding (partial build) instead of crashing the
    whole run.
    """
    for _ in range(2):
        try:
            result = provider.embed([text])
        except EmbeddingProviderError:
            continue
        return result.vectors[0], result.usage
    return None, {}


def build_pipeline(
    config: BuildConfig, artifact_store: ArtifactStore, cache: StageCache
) -> ManifestEnvelope:
    """Run discovery -> parse -> chunk -> scan -> embed -> manifest, deterministically."""
    started_at = config.created_at
    sources = discover_sources(config.root, config.namespace, config.discovery)

    state = _BuildState()
    for source in sources:
        if source.status == "tombstone":
            state.sources.append(source)
            continue
        _process_source(config, source, artifact_store, cache, state)

    if config.previous_sources:
        tombstones = compute_tombstones(config.previous_sources, [s.id for s in state.sources])
        state.sources.extend(tombstones)

    state.sources.sort(key=lambda item: item.uri)
    state.parse_runs.sort(key=lambda item: item.id)
    state.chunks.sort(key=lambda item: item.id)
    state.embeddings.sort(key=lambda item: item.id)
    state.assertions.sort(key=lambda item: item.id)
    state.artifacts.sort(key=lambda item: item.artifact_id)

    # `state.sources` is already sorted by `uri` above, so this is already
    # in deterministic order without a second sort.
    source_snapshot_hash = hash_canonical(
        [
            {"uri": source.uri, "content_hash": source.content_hash}
            for source in state.sources
            if source.status == "active"
        ]
    )
    pipeline_config_hash = hash_canonical(
        {
            "parser_config": config.parser_config,
            "chunker_name": config.chunker_name,
            "chunker_config": config.chunker_config,
            "embedding_normalization": config.embedding_normalization,
            "sandboxed": config.sandboxed,
        }
    )
    build_status: BuildStatus = "incomplete" if state.any_parse_failures else "complete"
    build_record = BuildRecord(
        build_id=config.build_id,
        status=build_status,
        source_snapshot_hash=source_snapshot_hash,
        pipeline_config_hash=pipeline_config_hash,
        started_at=started_at,
        completed_at=config.created_at,
        environment=_build_environment(),
        stages=[
            StageRecord(
                name=_DISCOVERY_STAGE_NAME,
                version=_DISCOVERY_STAGE_VERSION,
                input_count=len(sources),
                output_count=len(sources),
            ),
            StageRecord(
                name="ragledger.parse",
                version="1",
                input_count=len(sources),
                output_count=len(state.parse_runs),
            ),
            StageRecord(
                name="ragledger.chunk",
                version="1",
                input_count=len(state.parse_runs),
                output_count=len(state.chunks),
            ),
            StageRecord(
                name="ragledger.embed",
                version="1",
                input_count=len(state.chunks),
                output_count=len(state.embeddings),
            ),
        ],
        warnings=state.warnings,
    )

    manifest = build_manifest(
        namespace=config.namespace,
        created_at=config.created_at,
        build=build_record,
        ledger_version=config.ledger_version,
        sources=state.sources,
        parse_runs=state.parse_runs,
        chunks=state.chunks,
        embeddings=state.embeddings,
        assertions=state.assertions,
        artifacts=state.artifacts,
    )

    if config.signing_key is not None:
        manifest = sign_manifest(
            manifest, config.signing_key, signed_at=config.created_at, issuer=config.signing_issuer
        )
    return manifest


def _build_environment() -> BuildEnvironment:
    import platform

    return BuildEnvironment(os=platform.system(), python_version=platform.python_version())
