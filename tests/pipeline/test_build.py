"""Integration tests for `ragledger.pipeline.build.build_pipeline`.

Exercises the full discover -> parse -> chunk -> scan -> embed ->
manifest pipeline against the committed synthetic corpus
(`tests/fixtures/corpus/`), covering determinism (FR-082), stage
caching (section 10.1), failure behavior (section 10.2), the PII
embedding-block policy, signing, and tombstones (FR-017).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ragledger.core.artifacts import ArtifactStore
from ragledger.core.manifest import (
    canonical_manifest_bytes,
    manifest_to_dict,
    validate_manifest_document,
)
from ragledger.core.signing import VerificationOverall, verify_manifest
from ragledger.governance.acl import AclConfig, AclPathRule, TenantConfig, TenantPathRule
from ragledger.governance.license import LicenseConfig
from ragledger.governance.pii import PiiScanConfig
from ragledger.pipeline.build import BuildConfig, PiiPolicyConfig, build_pipeline
from ragledger.pipeline.cache import StageCache
from ragledger.pipeline.embedding import DeterministicLocalEmbeddingProvider

_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _base_config(root: Path, build_id: str = "bld_test", **overrides: object) -> BuildConfig:
    defaults: dict[str, object] = {
        "namespace": "test-namespace",
        "root": root,
        "build_id": build_id,
        "created_at": _CREATED_AT,
        "chunker_name": "hierarchical",
        "chunker_config": {"max_tokens": 40},
        "embedding_provider": DeterministicLocalEmbeddingProvider(dimension=8, seed=3),
        "pii_config": PiiScanConfig(workspace_secret=b"test-workspace-secret"),
        "license_config": LicenseConfig(repository_default="NOASSERTION"),
        "acl_config": AclConfig(path_rules=(AclPathRule("*", ("PUBLIC",)),)),
        "tenant_config": TenantConfig(path_rules=(TenantPathRule("*", "tenant", "acme"),)),
    }
    defaults.update(overrides)
    return BuildConfig(**defaults)  # type: ignore[arg-type]


def test_full_corpus_build_produces_a_schema_valid_manifest(
    corpus_dir: Path, tmp_path: Path
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(corpus_dir), store, cache)

    validate_manifest_document(manifest_to_dict(manifest))
    assert manifest.build.status == "complete"
    assert len(manifest.sources) == 7  # sample.csv/html/json/md/pdf/txt + spdx_header.txt
    assert manifest.statistics.source_count >= 6
    assert len(manifest.chunks) > 0
    assert len(manifest.embeddings) == len(manifest.chunks)
    assert manifest.index_bindings == []  # index binding creation is M5/connector scope


def test_every_source_media_type_gets_a_registered_parser(corpus_dir: Path, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(corpus_dir), store, cache)
    assert {run.status for run in manifest.parse_runs} == {"success"}
    parser_names = {run.parser_name for run in manifest.parse_runs}
    assert parser_names == {
        "ragledger.native_text",
        "ragledger.native_markdown",
        "ragledger.native_html",
        "ragledger.native_json",
        "ragledger.native_csv",
        "ragledger.pypdf",
    }


def test_determinism_two_runs_are_byte_identical(corpus_dir: Path, tmp_path: Path) -> None:
    # `reproducible=True` fixes ParseRecord.duration_seconds to 0.0 instead
    # of real (necessarily run-varying) wall-clock timing; see BuildConfig.reproducible.
    store = ArtifactStore(tmp_path / "artifacts")
    config = _base_config(corpus_dir, reproducible=True)
    first = build_pipeline(config, store, StageCache(tmp_path / "cache1"))
    second = build_pipeline(config, store, StageCache(tmp_path / "cache2"))
    assert canonical_manifest_bytes(first) == canonical_manifest_bytes(second)


def test_non_reproducible_mode_records_real_parse_duration_telemetry(
    corpus_dir: Path, tmp_path: Path
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    manifest = build_pipeline(_base_config(corpus_dir), store, StageCache(tmp_path / "cache"))
    assert any(run.duration_seconds > 0 for run in manifest.parse_runs)


def test_cache_hits_on_second_run_against_the_same_cache_directory(
    corpus_dir: Path, tmp_path: Path
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache_dir = tmp_path / "cache"
    build_pipeline(_base_config(corpus_dir), store, StageCache(cache_dir))
    second_cache = StageCache(cache_dir)
    build_pipeline(_base_config(corpus_dir), store, second_cache)
    assert second_cache.stats.hits > 0
    assert second_cache.stats.misses == 0


def test_license_effective_assertion_reflects_frontmatter(corpus_dir: Path, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(corpus_dir), store, cache)
    md_source = next(s for s in manifest.sources if s.uri == "file:sample.md")
    assert md_source.license_assertion_ids
    license_assertion = next(
        a for a in manifest.assertions if a.id == md_source.license_assertion_ids[0]
    )
    assert license_assertion.spdx_expression == "MIT"

    txt_source = next(s for s in manifest.sources if s.uri == "file:spdx_header.txt")
    header_assertion = next(
        a for a in manifest.assertions if a.id == txt_source.license_assertion_ids[0]
    )
    assert header_assertion.spdx_expression == "Apache-2.0"


def test_acl_and_tenant_assertions_attached_to_sources(corpus_dir: Path, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(corpus_dir), store, cache)
    for source in manifest.sources:
        assert source.declared_acl_assertion_id is not None
        assert source.declared_tenant == "acme"
    acl_assertions = [a for a in manifest.assertions if a.type == "ACL"]
    assert all(a.entries == ["PUBLIC"] for a in acl_assertions)


def test_pii_scan_finds_the_email_in_sample_txt(corpus_dir: Path, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(corpus_dir), store, cache)
    txt_source = next(s for s in manifest.sources if s.uri == "file:sample.txt")
    source_scan = next(
        a
        for a in manifest.assertions
        if a.type == "PII_SCAN" and a.subject_ref == txt_source.version_id
    )
    assert source_scan.status == "findings_detected"
    assert any(f.entity_type == "EMAIL_ADDRESS" for f in source_scan.findings)


def test_pii_policy_blocks_embedding_for_denied_entity_types(
    corpus_dir: Path, tmp_path: Path
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    config = _base_config(
        corpus_dir, pii_policy=PiiPolicyConfig(block_entity_types=frozenset({"EMAIL_ADDRESS"}))
    )
    manifest = build_pipeline(config, store, cache)

    embedded_chunk_ids = {e.chunk_id for e in manifest.embeddings}
    blocked_chunks = [
        chunk
        for chunk in manifest.chunks
        if any(
            assertion.id in chunk.pii_assertion_ids
            and assertion.type == "PII_SCAN"
            and any(f.entity_type == "EMAIL_ADDRESS" for f in assertion.findings)
            for assertion in manifest.assertions
        )
    ]
    assert blocked_chunks  # the corpus does contain at least one such chunk
    assert all(chunk.id not in embedded_chunk_ids for chunk in blocked_chunks)
    # a chunk with no blocked PII is still embedded
    assert len(embedded_chunk_ids) < len(manifest.chunks)
    assert len(embedded_chunk_ids) > 0


def test_duplicate_chunk_content_reported_as_a_quality_warning(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    # Two paragraphs with identical text (10 tokens each). max_tokens=10
    # combined with the 5-token marker paragraphs guarantees a marker can
    # never merge with a sentence paragraph (5 + 10 > 10), so the
    # hierarchical chunker emits each paragraph as its own chunk and the
    # two identical sentences become two chunks with the same raw_hash.
    (root / "dupes.txt").write_text(
        "Section marker one goes here.\n\n"
        "This exact sentence repeats twice in the document on purpose.\n\n"
        "Section marker two goes here.\n\n"
        "This exact sentence repeats twice in the document on purpose."
    )
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    config = _base_config(root, chunker_name="hierarchical", chunker_config={"max_tokens": 10})
    manifest = build_pipeline(config, store, cache)

    raw_hashes = [chunk.raw_hash for chunk in manifest.chunks]
    assert len(raw_hashes) != len(set(raw_hashes)), "fixture must actually produce a duplicate"

    quality_assertions = [a for a in manifest.assertions if a.type == "QUALITY"]
    codes = {w.code for assertion in quality_assertions for w in assertion.warnings}
    assert "DUPLICATE_CHUNK_CONTENT" in codes


def test_metadata_only_mode_produces_lineage_without_vectors(
    corpus_dir: Path, tmp_path: Path
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(corpus_dir, embedding_provider=None), store, cache)
    assert manifest.embeddings == []
    assert len(manifest.chunks) > 0


def test_no_parser_available_marks_build_incomplete_not_a_crash(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "data.bin").write_bytes(bytes(range(256)))  # sniffed as application/octet-stream
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(root), store, cache)
    assert manifest.build.status == "incomplete"
    assert manifest.parse_runs[0].status == "fail"
    assert manifest.parse_runs[0].warnings[0].code == "NO_PARSER_AVAILABLE"
    assert manifest.chunks == []


def test_broken_pdf_source_fails_parse_without_crashing_the_build(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "broken.pdf").write_bytes(b"%PDF-1.4\nnot actually a valid pdf body")
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(root), store, cache)
    assert manifest.build.status == "incomplete"
    assert manifest.parse_runs[0].status == "fail"


def test_tombstone_recorded_when_a_previously_seen_source_disappears(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "keep.txt").write_text("Stays around across both builds with enough words to chunk.")
    (root / "gone.txt").write_text("This one will be deleted before the second build runs here.")
    store = ArtifactStore(tmp_path / "artifacts")

    first = build_pipeline(
        _base_config(root, build_id="bld_1"), store, StageCache(tmp_path / "cache1")
    )
    (root / "gone.txt").unlink()

    second = build_pipeline(
        _base_config(
            root,
            build_id="bld_2",
            previous_sources=tuple(s for s in first.sources if s.status == "active"),
        ),
        store,
        StageCache(tmp_path / "cache2"),
    )
    tombstoned = [s for s in second.sources if s.status == "tombstone"]
    assert len(tombstoned) == 1
    assert tombstoned[0].uri == "file:gone.txt"
    active_uris = {s.uri for s in second.sources if s.status == "active"}
    assert active_uris == {"file:keep.txt"}


def test_signed_manifest_verifies_as_valid_trusted(corpus_dir: Path, tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(
        _base_config(corpus_dir, signing_key=private_key, signing_issuer="test-suite"), store, cache
    )
    assert len(manifest.signatures) == 1
    trusted_keys = {manifest.signatures[0].key_id: private_key.public_key()}
    result = verify_manifest(manifest, trusted_keys)
    assert result.overall == VerificationOverall.VALID_TRUSTED


def test_unsigned_manifest_is_still_a_valid_manifest(corpus_dir: Path, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(corpus_dir), store, cache)
    assert manifest.signatures == []


def test_raw_source_artifacts_are_stored_content_addressed(
    corpus_dir: Path, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(corpus_dir), store, cache)
    for source in manifest.sources:
        assert source.raw_artifact_ref is not None
        assert store.verify(source.content_hash)


def test_empty_source_root_produces_a_valid_empty_manifest(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    manifest = build_pipeline(_base_config(root), store, cache)
    assert manifest.sources == []
    assert manifest.build.status == "complete"
    validate_manifest_document(manifest_to_dict(manifest))
