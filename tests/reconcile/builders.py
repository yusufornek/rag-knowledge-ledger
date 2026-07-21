"""Shared manifest/connector builders for tests/reconcile.

`make_scenario` builds a small, fully synthetic, schema-valid manifest (one
source -> one parse run -> one chunk -> one embedding -> one index binding,
plus PII/license/ACL/tenant assertions) using only public
`ragledger.core`/`ragledger.governance` APIs -- the same building blocks a
real pipeline would use -- and a `NormalizedPoint` that, unmodified, would
match that manifest exactly. Individual tests take the manifest and/or the
point and mutate them to create a specific mismatch (a different
`source_version_id` for staleness, a broader `acl` for an ACL leak, an extra
or missing point, and so on).

`FunctionConnector` is a minimal `VectorTargetConnector` test double: it
replays whatever `NormalizedPoint`s a zero-argument factory callable
produces, so both a small fixed list and a large generated stream can share
one connector implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ragledger.connectors.base import (
    Checkpoint,
    ConnectionTestResult,
    ConnectorCapabilities,
    ConsistencyInfo,
    ConsistencyMode,
    NormalizedPoint,
    SnapshotCompleteness,
    TargetSchema,
    VectorFieldSchema,
    VectorTargetConnector,
    compute_payload_hash,
)
from ragledger.connectors.ndjson import NdjsonConnector, SnapshotHeader, write_snapshot
from ragledger.core import hashing, ids
from ragledger.core.manifest import build_manifest
from ragledger.core.models import (
    AclAssertion,
    BuildEnvironment,
    BuildRecord,
    ChunkRecord,
    EmbeddingModelInfo,
    EmbeddingRecord,
    IndexBinding,
    Integrity,
    LicenseAssertion,
    LicenseMethod,
    ManifestEnvelope,
    ParseRecord,
    PiiFinding,
    PiiScanAssertion,
    PiiScannerInfo,
    SourceRecord,
    Statistics,
    StructuralLocator,
    TenantAssertion,
    Tokenizer,
)

FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)
TARGET = "primary-target"
SCOPE = "support-kb"


# --------------------------------------------------------------------------
# Test-double connector
# --------------------------------------------------------------------------


class FunctionConnector(VectorTargetConnector[NormalizedPoint]):
    """Replays whatever `points_factory()` yields as a `VectorTargetConnector`.

    `points_factory` is called fresh every `iterate_points()` call (so a
    generator-backed factory can be iterated more than once, unlike a
    plain, already-exhausted generator object).
    """

    def __init__(
        self,
        points_factory: Callable[[], Iterator[NormalizedPoint]],
        *,
        target_id: str = TARGET,
        scope: str = SCOPE,
        vector_dimensions: dict[str, int] | None = None,
        completeness: SnapshotCompleteness = SnapshotCompleteness.COMPLETE,
        mode: ConsistencyMode = ConsistencyMode.STRICT_CONSISTENT,
        approx_point_count: int | None = None,
    ) -> None:
        self._points_factory = points_factory
        self._target_id = target_id
        self._scope = scope
        self._vector_dimensions = dict(vector_dimensions or {"default": 4})
        self._completeness = completeness
        self._mode = mode
        self._approx_point_count = approx_point_count
        self._consistency: ConsistencyInfo | None = None

    def validate_configuration(self) -> None:
        return None

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(ok=True, message="reachable")

    def inspect_target_schema(self) -> TargetSchema:
        return TargetSchema(
            target_id=self._target_id,
            scope=self._scope,
            vector_fields=tuple(
                VectorFieldSchema(name=name, dimension=dim)
                for name, dim in self._vector_dimensions.items()
            ),
            point_id_type="string",
            approx_point_count=self._approx_point_count,
        )

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target_type="function-test-double",
            supports_resume=False,
            supports_vector_hash=True,
            supports_sampling=False,
            max_page_size=None,
            consistency_modes=(self._mode,),
        )

    def estimate_count(self) -> int | None:
        return self._approx_point_count

    def normalize_point(
        self, raw: NormalizedPoint, *, include_vectors: bool = False
    ) -> NormalizedPoint:
        return raw

    def iterate_points(
        self,
        *,
        checkpoint: Checkpoint | None = None,
        projection: Sequence[str] | None = None,
        include_vectors: bool = False,
    ) -> Iterator[NormalizedPoint]:
        count = 0
        start_count = self._approx_point_count
        for point in self._points_factory():
            count += 1
            yield point
        self._consistency = ConsistencyInfo(
            mode=self._mode,
            completeness=self._completeness,
            start_count=start_count if start_count is not None else count,
            end_count=count,
            observed_count=count,
        )

    def get_consistency_info(self) -> ConsistencyInfo:
        if self._consistency is None:
            raise RuntimeError("iterate_points has not completed a pass yet")
        return self._consistency

    def close(self) -> None:
        return None


def list_connector(points: Sequence[NormalizedPoint], **kwargs: Any) -> FunctionConnector:
    materialized = list(points)
    kwargs.setdefault("approx_point_count", len(materialized))
    return FunctionConnector(lambda: iter(materialized), **kwargs)


def write_ndjson_snapshot(
    path: Path,
    points: Sequence[NormalizedPoint],
    *,
    target: str = TARGET,
    scope: str = SCOPE,
    vector_dimensions: dict[str, int] | None = None,
) -> NdjsonConnector:
    """Write `points` as a `.ndjson.zst` snapshot and return an open `NdjsonConnector` for it."""
    header = SnapshotHeader(
        target_id=target,
        scope=scope,
        target_type="ndjson-test-fixture",
        vector_names=list((vector_dimensions or {"default": 4}).keys()),
        vector_dimensions=vector_dimensions or {"default": 4},
        started_at=FIXED_TIME,
        connector_version="test-1",
        consistency_mode=ConsistencyMode.STRICT_CONSISTENT.value,
    )
    write_snapshot(path, header, points, finished_at=FIXED_TIME)
    return NdjsonConnector(path, clock=lambda: FIXED_TIME)


# --------------------------------------------------------------------------
# Synthetic manifest scenario
# --------------------------------------------------------------------------


@dataclass
class Scenario:
    manifest: ManifestEnvelope
    binding: IndexBinding
    embedding: EmbeddingRecord
    chunk: ChunkRecord
    source: SourceRecord
    matching_point: NormalizedPoint
    target: str
    scope: str
    payload_projection: dict[str, Any]


def _build_record(build_id: str, status: str = "complete") -> BuildRecord:
    return BuildRecord(
        build_id=build_id,
        status=status,
        source_snapshot_hash=hashing.hash_canonical({"build_id": build_id}),
        pipeline_config_hash=hashing.hash_canonical({"pipeline": "reconcile-tests"}),
        started_at=FIXED_TIME,
        completed_at=FIXED_TIME,
        environment=BuildEnvironment(python_version="3.13.0"),
    )


def make_scenario(
    *,
    build_id: str = "bld_reconcile_test",
    namespace: str = "reconcile-tests",
    uri: str = "file:documents/refund-policy.md",
    body: bytes = b"# Refund policy\n\nRefunds are available within 30 days.\n",
    chunk_text: str = "Refunds are available within 30 days.",
    target: str = TARGET,
    scope: str = SCOPE,
    dimension: int = 4,
    tenant_value: str | None = "tenant-a",
    acl_entries: tuple[str, ...] | None = ("GROUP:finance",),
    license_expression: str = "MIT",
    license_method: LicenseMethod = "frontmatter",
    pii_findings: tuple[tuple[str, float], ...] = (),
    build_status: str = "complete",
    point_id: str = "point-000001",
    signed: bool = False,
) -> Scenario:
    """Build one small, schema-valid, self-consistent manifest + matching point."""
    content_hash = hashing.hash_raw_bytes(body)
    source_id = ids.source_id(namespace, uri)
    version_id = ids.source_version_id(source_id, content_hash)

    tenant_assertion = None
    if tenant_value is not None:
        tenant_assertion = TenantAssertion(
            id="ast_tenant_" + version_id[-16:],
            subject_ref=version_id,
            created_at=FIXED_TIME,
            tenant_hash=hashing.hash_canonical({"tenant": tenant_value}),
            tenant_key="tenant",
            tenant_value=tenant_value,
        )
    acl_assertion = None
    if acl_entries is not None:
        acl_assertion = AclAssertion(
            id="ast_acl_" + version_id[-16:],
            subject_ref=version_id,
            created_at=FIXED_TIME,
            acl_hash=hashing.hash_canonical(list(acl_entries)),
            entries=list(acl_entries),
        )

    source = SourceRecord(
        id=source_id,
        version_id=version_id,
        namespace=namespace,
        uri=uri,
        media_type="text/markdown",
        size_bytes=len(body),
        content_hash=content_hash,
        source_system="local_fs",
        status="active",
        discovered_by="unknown",
        declared_tenant=tenant_value,
    )

    parser_config_hash = hashing.hash_canonical({"parser": "native_markdown", "version": "1"})
    parse_run_id = ids.parse_run_id(version_id, parser_config_hash)
    parse_run = ParseRecord(
        id=parse_run_id,
        source_version_id=version_id,
        parser_name="native_markdown",
        parser_version="0.1.0",
        config_hash=parser_config_hash,
        status="success",
        parsed_artifact_ref="art_" + content_hash[:16],
        duration_seconds=0.01,
    )

    locator = StructuralLocator(kind="document_span", page_start=1, page_end=1, ordinal=0)
    chunker_config_hash = hashing.hash_canonical({"strategy": "line_based", "max_tokens": 200})
    chunk_hash = hashing.hash_text(chunk_text)
    chunk_id = ids.chunk_id(
        parse_run_id, chunker_config_hash, locator.model_dump(mode="json"), chunk_hash
    )

    license_assertion = LicenseAssertion(
        id="lic_" + chunk_id[-16:],
        subject_ref=version_id,
        created_at=FIXED_TIME,
        spdx_expression=license_expression,
        method=license_method,
    )
    pii_assertion = None
    if pii_findings:
        pii_assertion = PiiScanAssertion(
            id="pii_" + chunk_id[-16:],
            subject_ref=chunk_id,
            created_at=FIXED_TIME,
            scanner=PiiScannerInfo(name="ragledger-deterministic-pii-scanner", version="1"),
            status="findings_detected",
            findings=[
                PiiFinding(
                    entity_type=entity,
                    confidence=confidence,
                    start=0,
                    end=5,
                    masked_preview="***",
                    recognizer_id="test",
                    recognizer_version="1",
                )
                for entity, confidence in pii_findings
            ],
        )

    chunk = ChunkRecord(
        id=chunk_id,
        source_version_id=version_id,
        parse_run_id=parse_run_id,
        locator=locator,
        raw_hash=chunk_hash,
        contextualized_hash=chunk_hash,
        token_count=7,
        tokenizer=Tokenizer(name="cl100k_base", revision="1"),
        license_assertion_ids=[license_assertion.id],
        pii_assertion_ids=[pii_assertion.id] if pii_assertion else [],
        acl_assertion_ids=[acl_assertion.id] if acl_assertion else [],
    )

    embedding_config_hash = hashing.hash_canonical(
        {"provider": "local", "model": "test-embedder", "dimension": dimension}
    )
    embedding_id = ids.embedding_id(chunk_id, chunk_hash, embedding_config_hash)
    embedding = EmbeddingRecord(
        id=embedding_id,
        chunk_id=chunk_id,
        model=EmbeddingModelInfo(provider="local", name="test-embedder", revision="1"),
        dimension=dimension,
        dtype="float32",
        normalization="l2",
        distance_expectation="cosine",
        contextualized_hash=chunk_hash,
        generated_at=FIXED_TIME,
    )

    payload_projection: dict[str, Any] = {
        "source_id": source_id,
        "source_version_id": version_id,
        "chunk_id": chunk_id,
        "embedding_id": embedding_id,
    }
    if tenant_value is not None:
        payload_projection["tenant"] = tenant_value
    if acl_entries is not None:
        payload_projection["acl"] = list(acl_entries)
    payload_hash = compute_payload_hash(payload_projection)

    binding = IndexBinding(
        id=ids.index_binding_id(target, embedding_id, point_id),
        target=target,
        namespace=scope,
        point_id=point_id,
        embedding_id=embedding_id,
        expected_payload_hash=payload_hash,
        expected_payload_projection=payload_projection,
        tenant_projection=tenant_value,
        acl_projection=list(acl_entries) if acl_entries is not None else None,
        write_status="written",
    )

    assertions = [
        assertion
        for assertion in (license_assertion, acl_assertion, tenant_assertion, pii_assertion)
        if assertion is not None
    ]

    manifest = build_manifest(
        namespace=namespace,
        created_at=FIXED_TIME,
        build=_build_record(build_id, build_status),
        ledger_version="0.1.0",
        sources=[source],
        parse_runs=[parse_run],
        chunks=[chunk],
        embeddings=[embedding],
        index_bindings=[binding],
        assertions=assertions,
    )

    if signed:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from ragledger.core.signing import sign_manifest

        private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        manifest = sign_manifest(
            manifest, private_key, signed_at=FIXED_TIME, issuer="reconcile-tests"
        )

    matching_point = NormalizedPoint(
        target_id=target,
        scope=scope,
        point_id=point_id,
        vector_names=["default"],
        vector_dimensions={"default": dimension},
        payload_projection=dict(payload_projection),
        payload_hash=payload_hash,
        source_id=source_id,
        source_version_id=version_id,
        chunk_id=chunk_id,
        embedding_id=embedding_id,
        acl=list(acl_entries) if acl_entries is not None else None,
        tenant=tenant_value,
        observed_at=FIXED_TIME,
        raw_locator=f"test:{scope}#{point_id}",
    )

    return Scenario(
        manifest=manifest,
        binding=binding,
        embedding=embedding,
        chunk=chunk,
        source=source,
        matching_point=matching_point,
        target=target,
        scope=scope,
        payload_projection=payload_projection,
    )


# --------------------------------------------------------------------------
# Bulk synthetic dataset generator (equivalence and scale tests)
# --------------------------------------------------------------------------


def make_bulk_dataset(
    matched_count: int,
    *,
    orphan_count: int = 0,
    missing_count: int = 0,
    target: str = TARGET,
    scope: str = SCOPE,
    dimension: int = 4,
    stale_every: int = 0,
    acl_leak_every: int = 0,
    namespace: str = "bulk-tests",
) -> tuple[ManifestEnvelope, list[NormalizedPoint]]:
    """Generate `matched_count` expected/observed pairs (plus optional
    orphan-only and missing-only extras) sharing one source, in-stream,
    without `build_manifest`'s schema-validation pass -- that pass alone
    would dominate wall-clock time at 100k+ records, and this generator's
    job is to stress reconciliation's own matching/spill machinery, not
    manifest-build-time schema validation (a separate, already-tested
    concern; see `ragledger.core.manifest`). Every record is still a real,
    individually-pydantic-validated model, so reconciliation exercises the
    exact same typed objects it would see from `build_manifest` output.

    `stale_every`: every Nth matched point gets an observed
    `source_version_id` that does not match the manifest's current source
    version (a `STALE_SOURCE` finding). `acl_leak_every`: every Nth matched
    point's observed ACL is widened to `["PUBLIC"]`.
    """
    uri = "file:documents/bulk.md"
    content_hash = hashing.hash_raw_bytes(b"bulk synthetic content")
    source_id = ids.source_id(namespace, uri)
    version_id = ids.source_version_id(source_id, content_hash)
    stale_version_id = "ver_" + ("stale" * 12)[:52]

    source = SourceRecord(
        id=source_id,
        version_id=version_id,
        namespace=namespace,
        uri=uri,
        media_type="text/plain",
        size_bytes=32,
        content_hash=content_hash,
        source_system="synthetic",
        status="active",
    )
    parser_config_hash = hashing.hash_canonical({"parser": "synthetic"})
    parse_run_id = ids.parse_run_id(version_id, parser_config_hash)
    parse_run = ParseRecord(
        id=parse_run_id,
        source_version_id=version_id,
        parser_name="synthetic",
        parser_version="1",
        status="success",
        parsed_artifact_ref="art_synthetic",
        duration_seconds=0.0,
    )
    chunker_config_hash = hashing.hash_canonical({"strategy": "synthetic"})
    embedding_config_hash = hashing.hash_canonical(
        {"provider": "synthetic", "dimension": dimension}
    )
    acl_entries = ["GROUP:finance"]

    chunks: list[ChunkRecord] = []
    embeddings: list[EmbeddingRecord] = []
    bindings: list[IndexBinding] = []
    points: list[NormalizedPoint] = []

    total_expected = matched_count + missing_count
    for i in range(total_expected):
        locator = StructuralLocator(kind="synthetic", ordinal=i)
        chunk_text = f"synthetic chunk body {i}"
        chunk_hash = hashing.hash_text(chunk_text)
        chunk_id = ids.chunk_id(
            parse_run_id, chunker_config_hash, locator.model_dump(mode="json"), chunk_hash
        )
        chunks.append(
            ChunkRecord(
                id=chunk_id,
                source_version_id=version_id,
                parse_run_id=parse_run_id,
                locator=locator,
                raw_hash=chunk_hash,
                contextualized_hash=chunk_hash,
                token_count=4,
                tokenizer=Tokenizer(name="cl100k_base", revision="1"),
            )
        )
        embedding_id = ids.embedding_id(chunk_id, chunk_hash, embedding_config_hash)
        embeddings.append(
            EmbeddingRecord(
                id=embedding_id,
                chunk_id=chunk_id,
                model=EmbeddingModelInfo(provider="synthetic", name="bulk-embedder", revision="1"),
                dimension=dimension,
                dtype="float32",
                contextualized_hash=chunk_hash,
                generated_at=FIXED_TIME,
            )
        )
        point_id = f"point-{i:08d}"
        payload_projection: dict[str, Any] = {
            "source_id": source_id,
            "source_version_id": version_id,
            "chunk_id": chunk_id,
            "embedding_id": embedding_id,
            "acl": acl_entries,
        }
        payload_hash = compute_payload_hash(payload_projection)
        binding_id = ids.index_binding_id(target, embedding_id, point_id)
        bindings.append(
            IndexBinding(
                id=binding_id,
                target=target,
                namespace=scope,
                point_id=point_id,
                embedding_id=embedding_id,
                expected_payload_hash=payload_hash,
                expected_payload_projection=payload_projection,
                acl_projection=acl_entries,
                write_status="written",
            )
        )
        if i < matched_count:
            observed_source_version = version_id
            observed_acl = list(acl_entries)
            if stale_every and (i + 1) % stale_every == 0:
                observed_source_version = stale_version_id
            if acl_leak_every and (i + 1) % acl_leak_every == 0:
                observed_acl = ["PUBLIC"]
            observed_projection = dict(payload_projection)
            observed_projection["source_version_id"] = observed_source_version
            observed_projection["acl"] = observed_acl
            points.append(
                NormalizedPoint(
                    target_id=target,
                    scope=scope,
                    point_id=point_id,
                    vector_names=["default"],
                    vector_dimensions={"default": dimension},
                    payload_projection=observed_projection,
                    payload_hash=compute_payload_hash(observed_projection),
                    source_id=source_id,
                    source_version_id=observed_source_version,
                    chunk_id=chunk_id,
                    embedding_id=embedding_id,
                    acl=observed_acl,
                    observed_at=FIXED_TIME,
                    raw_locator=f"synthetic:{scope}#{point_id}",
                )
            )

    for j in range(orphan_count):
        point_id = f"orphan-{j:08d}"
        points.append(
            NormalizedPoint(
                target_id=target,
                scope=scope,
                point_id=point_id,
                vector_names=["default"],
                vector_dimensions={"default": dimension},
                payload_projection={},
                payload_hash=hashing.hash_canonical({"orphan": j}),
                observed_at=FIXED_TIME,
                raw_locator=f"synthetic:{scope}#{point_id}",
            )
        )

    build = BuildRecord(
        build_id="bld_bulk_synthetic",
        status="complete",
        source_snapshot_hash=hashing.hash_canonical({"matched_count": matched_count}),
        pipeline_config_hash=hashing.hash_canonical({"pipeline": "bulk-synthetic"}),
        started_at=FIXED_TIME,
        completed_at=FIXED_TIME,
        environment=BuildEnvironment(python_version="3.13.0"),
    )
    manifest = ManifestEnvelope(
        created_at=FIXED_TIME,
        ledger_version="0.1.0",
        namespace=namespace,
        build=build,
        sources=[source],
        parse_runs=[parse_run],
        chunks=chunks,
        embeddings=embeddings,
        index_bindings=bindings,
        assertions=[],
        artifacts=[],
        statistics=Statistics(
            source_count=1,
            chunk_count=len(chunks),
            embedding_count=len(embeddings),
            index_binding_count=len(bindings),
            assertion_count=0,
            artifact_count=0,
        ),
        integrity=Integrity(manifest_hash="0" * 64),
        signatures=[],
    )
    return manifest, points
