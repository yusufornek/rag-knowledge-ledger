"""Builders for the golden manifest corpus (PROJECT_SPEC.md section 42.1).

Each builder here constructs a small, fully synthetic manifest using
only `ragledger.core` public functions -- the same code path a real
pipeline would use -- with every timestamp, hash input, and key
supplied explicitly so that calling a builder twice always produces
byte-identical output. `tests/core/test_golden_manifests.py` uses these
builders both for an in-process determinism check and to compare
against the committed canonical JSON bytes under
`tests/fixtures/golden/`.

Not a test module itself (no `test_` prefix), so pytest does not
collect it; it is imported by `test_golden_manifests.py` and by the
one-off regeneration script.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ragledger.core import hashing, ids
from ragledger.core.manifest import build_manifest
from ragledger.core.models import (
    AclAssertion,
    ArtifactRef,
    BuildEnvironment,
    BuildRecord,
    ChunkRecord,
    EmbeddingModelInfo,
    EmbeddingRecord,
    IndexBinding,
    LicenseAssertion,
    ManifestEnvelope,
    ParseRecord,
    PiiScanAssertion,
    PiiScannerInfo,
    QualityAssertion,
    SourceRecord,
    StructuralLocator,
    TenantAssertion,
    Tokenizer,
    WarningRecord,
)
from ragledger.core.signing import sign_manifest

FIXED_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
"""A fixed instant, unrelated to wall-clock "now", used everywhere a
golden fixture needs a timestamp. Never `datetime.now()`: the whole
point of a golden fixture is that it is the same on every run, on every
machine, forever."""

FIXED_SIGNING_SEED = bytes(range(32))
"""A fixed 32-byte Ed25519 private key seed for the signed golden
fixture. Ed25519 signing is deterministic (see
`docs/architecture/adr/0002-signing-algorithm.md`), so a fixed seed plus
a fixed message always produces the same signature bytes."""


def _build_record(build_id: str) -> BuildRecord:
    return BuildRecord(
        build_id=build_id,
        status="complete",
        source_snapshot_hash=hashing.hash_canonical({"build_id": build_id}),
        pipeline_config_hash=hashing.hash_canonical({"pipeline": "golden-fixture"}),
        started_at=FIXED_CREATED_AT,
        completed_at=FIXED_CREATED_AT,
        environment=BuildEnvironment(python_version="3.13.0"),
    )


def build_minimal_manifest() -> ManifestEnvelope:
    """A minimal but complete manifest: one source, one parse run, one chunk.

    Demonstrates the "unknown" convention for unobserved metadata
    (`SourceRecord.discovered_by`).
    """
    namespace = "golden-fixtures"
    uri = "file:documents/refund-policy.md"
    raw_bytes = b"# Refund policy\n\nRefunds are available within 30 days of purchase.\n"
    content_hash = hashing.hash_raw_bytes(raw_bytes)

    source_id = ids.source_id(namespace, uri)
    version_id = ids.source_version_id(source_id, content_hash)

    source = SourceRecord(
        id=source_id,
        version_id=version_id,
        namespace=namespace,
        uri=uri,
        media_type="text/markdown",
        size_bytes=len(raw_bytes),
        content_hash=content_hash,
        source_system="local_fs",
        status="active",
        discovered_by="unknown",
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
        duration_seconds=0.012,
    )

    locator = StructuralLocator(kind="document_span", page_start=1, page_end=1, ordinal=0)
    chunker_config_hash = hashing.hash_canonical({"strategy": "line_based", "max_tokens": 200})
    chunk_text = "Refunds are available within 30 days of purchase."
    chunk_hash = hashing.hash_text(chunk_text)
    chunk_id = ids.chunk_id(
        parse_run_id, chunker_config_hash, locator.model_dump(mode="json"), chunk_hash
    )
    chunk = ChunkRecord(
        id=chunk_id,
        source_version_id=version_id,
        parse_run_id=parse_run_id,
        locator=locator,
        raw_hash=chunk_hash,
        contextualized_hash=chunk_hash,
        token_count=9,
        tokenizer=Tokenizer(name="cl100k_base", revision="1"),
    )

    return build_manifest(
        namespace=namespace,
        created_at=FIXED_CREATED_AT,
        build=_build_record("bld_golden_minimal"),
        ledger_version="0.1.0",
        sources=[source],
        parse_runs=[parse_run],
        chunks=[chunk],
    )


def build_full_pipeline_manifest() -> ManifestEnvelope:
    """A manifest exercising every record type: source through index binding,
    all five assertion types, and one artifact reference.
    """
    namespace = "golden-fixtures"
    uri = "file:documents/privacy-notice.md"
    raw_bytes = (
        b"# Privacy notice\n\nContact privacy@example.com for data requests.\n"
        b"This document is licensed under CC-BY-4.0.\n"
    )
    content_hash = hashing.hash_raw_bytes(raw_bytes)

    source_id = ids.source_id(namespace, uri)
    version_id = ids.source_version_id(source_id, content_hash)

    artifact_ref = ArtifactRef(
        artifact_id="art_" + content_hash,
        media_type="text/markdown",
        sha256=content_hash,
        size_bytes=len(raw_bytes),
        compression="none",
        encryption="none",
        locator=f"artifacts/{content_hash}",
        sensitivity="internal",
    )

    source = SourceRecord(
        id=source_id,
        version_id=version_id,
        namespace=namespace,
        uri=uri,
        media_type="text/markdown",
        size_bytes=len(raw_bytes),
        content_hash=content_hash,
        source_system="local_fs",
        status="active",
        discovered_by="cli_scan",
        declared_tenant="tenant-a",
        raw_artifact_ref=artifact_ref.artifact_id,
        license_assertion_ids=["lic_privacy_notice"],
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
        parsed_artifact_ref=artifact_ref.artifact_id,
        duration_seconds=0.02,
        warnings=[WarningRecord(code="SHORT_DOCUMENT", message="Document has few paragraphs.")],
    )

    chunker_config_hash = hashing.hash_canonical({"strategy": "line_based", "max_tokens": 200})

    locator_a = StructuralLocator(
        kind="document_span", page_start=1, page_end=1, heading_path=["Privacy notice"], ordinal=0
    )
    text_a = "Contact privacy@example.com for data requests."
    hash_a = hashing.hash_text(text_a)
    chunk_id_a = ids.chunk_id(
        parse_run_id, chunker_config_hash, locator_a.model_dump(mode="json"), hash_a
    )
    chunk_a = ChunkRecord(
        id=chunk_id_a,
        source_version_id=version_id,
        parse_run_id=parse_run_id,
        locator=locator_a,
        raw_hash=hash_a,
        contextualized_hash=hash_a,
        token_count=7,
        tokenizer=Tokenizer(name="cl100k_base", revision="1"),
    )

    locator_b = StructuralLocator(
        kind="document_span", page_start=1, page_end=1, heading_path=["Privacy notice"], ordinal=1
    )
    text_b = "This document is licensed under CC-BY-4.0."
    hash_b = hashing.hash_text(text_b)
    chunk_id_b = ids.chunk_id(
        parse_run_id, chunker_config_hash, locator_b.model_dump(mode="json"), hash_b
    )
    chunk_b = ChunkRecord(
        id=chunk_id_b,
        source_version_id=version_id,
        parse_run_id=parse_run_id,
        locator=locator_b,
        raw_hash=hash_b,
        contextualized_hash=hash_b,
        token_count=7,
        tokenizer=Tokenizer(name="cl100k_base", revision="1"),
        neighbor_ids=[chunk_id_a],
    )

    embedding_config_hash = hashing.hash_canonical(
        {"provider": "local", "model": "test-embedder", "dimension": 4}
    )
    embedding_id = ids.embedding_id(chunk_id_a, hash_a, embedding_config_hash)
    embedding = EmbeddingRecord(
        id=embedding_id,
        chunk_id=chunk_id_a,
        model=EmbeddingModelInfo(provider="local", name="test-embedder", revision="1"),
        dimension=4,
        dtype="float32",
        normalization="l2",
        distance_expectation="cosine",
        contextualized_hash=hash_a,
        generated_at=FIXED_CREATED_AT,
    )

    payload_hash = hashing.hash_canonical({"chunk_id": chunk_id_a, "tenant": "tenant-a"})
    point_id = "point-000001"
    binding_id = ids.index_binding_id("primary-qdrant", embedding_id, point_id)
    binding = IndexBinding(
        id=binding_id,
        target="primary-qdrant",
        namespace="support-kb",
        point_id=point_id,
        embedding_id=embedding_id,
        expected_payload_hash=payload_hash,
        write_status="pending",
    )

    pii_assertion = PiiScanAssertion(
        id="ast_pii_" + chunk_id_a[-16:],
        subject_ref=chunk_id_a,
        created_at=FIXED_CREATED_AT,
        scanner=PiiScannerInfo(name="presidio", version="2.2"),
        status="findings_detected",
        findings=[
            {
                "entity_type": "EMAIL_ADDRESS",
                "confidence": 0.95,
                "start": 8,
                "end": 29,
                "recognizer_id": "email_recognizer",
                "recognizer_version": "1",
            }
        ],
    )
    license_assertion = LicenseAssertion(
        id="lic_privacy_notice",
        subject_ref=version_id,
        created_at=FIXED_CREATED_AT,
        spdx_expression="CC-BY-4.0",
        method="frontmatter",
        confidence=1.0,
    )
    acl_assertion = AclAssertion(
        id="ast_acl_" + version_id[-16:],
        subject_ref=version_id,
        created_at=FIXED_CREATED_AT,
        acl_hash=hashing.hash_canonical(["PUBLIC"]),
        entries=["PUBLIC"],
    )
    tenant_assertion = TenantAssertion(
        id="ast_tenant_" + version_id[-16:],
        subject_ref=version_id,
        created_at=FIXED_CREATED_AT,
        tenant_hash=hashing.hash_canonical({"tenant": "tenant-a"}),
        tenant_key="tenant",
        tenant_value="tenant-a",
    )
    quality_assertion = QualityAssertion(
        id="ast_quality_" + parse_run_id[-16:],
        subject_ref=parse_run_id,
        created_at=FIXED_CREATED_AT,
        warnings=[WarningRecord(code="SHORT_DOCUMENT")],
    )

    return build_manifest(
        namespace=namespace,
        created_at=FIXED_CREATED_AT,
        build=_build_record("bld_golden_full"),
        ledger_version="0.1.0",
        sources=[source],
        parse_runs=[parse_run],
        chunks=[chunk_a, chunk_b],
        embeddings=[embedding],
        index_bindings=[binding],
        assertions=[
            pii_assertion,
            license_assertion,
            acl_assertion,
            tenant_assertion,
            quality_assertion,
        ],
        artifacts=[artifact_ref],
    )


def build_signed_manifest() -> ManifestEnvelope:
    """The minimal manifest, signed with a fixed, deterministic Ed25519 key.

    Confirms that determinism survives the signing step: Ed25519 does
    not use a random per-signature nonce, so signing the same content
    with the same key on two different runs produces the same signature
    bytes, and therefore the same canonical manifest bytes.
    """
    private_key = Ed25519PrivateKey.from_private_bytes(FIXED_SIGNING_SEED)
    unsigned = build_minimal_manifest()
    return sign_manifest(
        unsigned, private_key, signed_at=FIXED_CREATED_AT, issuer="golden-fixtures"
    )
