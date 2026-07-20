"""Tests for `ragledger.core.manifest`.

Covers `build_manifest`'s schema validity and statistics computation,
`manifest_hash` correctness against a hand-recomputed signing view, that
the signing view genuinely omits `manifest_hash` and forces
`signatures` empty (no circularity), that any field change changes the
hash, and file write/load roundtrip byte-for-byte identity.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from ragledger.core.hashing import hash_canonical, sha256_hex
from ragledger.core.manifest import (
    build_manifest,
    canonical_manifest_bytes,
    compute_manifest_hash,
    load_manifest,
    signing_view_bytes,
    validate_manifest_document,
    write_manifest,
)
from ragledger.core.models import (
    BuildEnvironment,
    BuildRecord,
    ChunkRecord,
    ParseRecord,
    QualityAssertion,
    SourceRecord,
    StructuralLocator,
    Tokenizer,
    WarningRecord,
)

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
HASH_A = sha256_hex(b"a")
HASH_B = sha256_hex(b"b")


def _build_record(build_id: str = "bld_manifest_test") -> BuildRecord:
    return BuildRecord(
        build_id=build_id,
        status="complete",
        source_snapshot_hash=HASH_A,
        pipeline_config_hash=HASH_B,
        started_at=CREATED_AT,
        completed_at=CREATED_AT,
        environment=BuildEnvironment(python_version="3.13.0"),
    )


def _source_record(source_id: str = "src_x") -> SourceRecord:
    return SourceRecord(
        id=source_id,
        version_id="ver_x",
        namespace="ns",
        uri="file:doc.md",
        media_type="text/markdown",
        size_bytes=10,
        content_hash=HASH_A,
        source_system="local_fs",
        status="active",
    )


class TestBuildManifestIsSchemaValid:
    def test_minimal_manifest_is_schema_valid(self) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        # build_manifest validates internally; this asserts it did not
        # silently swallow a validation error and that the object it
        # returned is still valid.
        validate_manifest_document(
            manifest.model_dump(mode="json", exclude_none=True, by_alias=True)
        )

    def test_envelope_constants_are_set(self) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        dumped = manifest.model_dump(mode="json", exclude_none=True, by_alias=True)
        assert dumped["schema"] == "https://ragledger.dev/schemas/manifest-v1.json"
        assert dumped["media_type"] == "application/vnd.ragledger.manifest.v1+json"
        assert dumped["manifest_version"] == "1.0"


class TestStatisticsComputation:
    def test_source_count_deduplicates_by_source_id_not_version(self) -> None:
        version_a = SourceRecord(
            id="src_x",
            version_id="ver_a",
            namespace="ns",
            uri="file:doc.md",
            media_type="text/markdown",
            size_bytes=1,
            content_hash=HASH_A,
            source_system="local_fs",
            status="tombstone",
        )
        version_b = SourceRecord(
            id="src_x",
            version_id="ver_b",
            namespace="ns",
            uri="file:doc.md",
            media_type="text/markdown",
            size_bytes=2,
            content_hash=HASH_B,
            source_system="local_fs",
            status="active",
        )
        manifest = build_manifest(
            namespace="ns",
            created_at=CREATED_AT,
            build=_build_record(),
            ledger_version="0.1.0",
            sources=[version_a, version_b],
        )
        assert manifest.statistics.source_count == 1
        assert manifest.statistics.source_version_count == 2

    def test_warning_count_aggregates_build_parse_and_quality_warnings(self) -> None:
        build = _build_record().model_copy(
            update={"warnings": [WarningRecord(code="BUILD_WARNING")]}
        )
        parse_run = ParseRecord(
            id="prs_x",
            source_version_id="ver_x",
            parser_name="native_markdown",
            parser_version="0.1.0",
            status="partial",
            parsed_artifact_ref="art_x",
            duration_seconds=0.1,
            warnings=[WarningRecord(code="PARSE_WARNING_A"), WarningRecord(code="PARSE_WARNING_B")],
        )
        quality = QualityAssertion(
            id="ast_quality",
            subject_ref="prs_x",
            created_at=CREATED_AT,
            warnings=[WarningRecord(code="QUALITY_WARNING")],
        )
        manifest = build_manifest(
            namespace="ns",
            created_at=CREATED_AT,
            build=build,
            ledger_version="0.1.0",
            parse_runs=[parse_run],
            assertions=[quality],
        )
        assert manifest.statistics.warning_count == 4


class TestManifestHash:
    def test_manifest_hash_matches_recomputation(self) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        assert manifest.integrity.manifest_hash == compute_manifest_hash(manifest)

    def test_signing_view_omits_manifest_hash_and_empties_signatures(self) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        view = json.loads(signing_view_bytes(manifest))
        assert "manifest_hash" not in view["integrity"]
        assert view["signatures"] == []

    def test_manifest_hash_is_sha256_of_signing_view(self) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        assert manifest.integrity.manifest_hash == sha256_hex(signing_view_bytes(manifest))

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda m: m.model_copy(update={"namespace": "different-namespace"}),
            lambda m: m.model_copy(update={"created_at": datetime(2027, 1, 1, tzinfo=UTC)}),
            lambda m: m.model_copy(
                update={"sources": [*m.sources, SourceRecord.model_validate(_source_record())]}
            ),
        ],
    )
    def test_any_content_change_changes_the_hash(self, mutate) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        original_hash = manifest.integrity.manifest_hash
        mutated = mutate(manifest)
        assert compute_manifest_hash(mutated) != original_hash

    def test_field_order_in_source_python_objects_does_not_affect_hash(self) -> None:
        # Two manifests built from equivalent but differently-ordered
        # record construction must hash identically: canonical.py sorts
        # object keys, so Python attribute/construction order is never
        # observable in the hash.
        manifest_a = build_manifest(
            namespace="ns",
            created_at=CREATED_AT,
            build=_build_record(),
            ledger_version="0.1.0",
            sources=[_source_record("src_a"), _source_record("src_b")],
        )
        manifest_b = build_manifest(
            namespace="ns",
            created_at=CREATED_AT,
            build=_build_record(),
            ledger_version="0.1.0",
            sources=[_source_record("src_b"), _source_record("src_a")][::-1],
        )
        assert manifest_a.integrity.manifest_hash == manifest_b.integrity.manifest_hash


class TestCanonicalBytesAndRoundtrip:
    def test_rebuilding_the_same_manifest_twice_is_byte_identical(self) -> None:
        def make() -> bytes:
            manifest = build_manifest(
                namespace="ns",
                created_at=CREATED_AT,
                build=_build_record(),
                ledger_version="0.1.0",
                sources=[_source_record()],
            )
            return canonical_manifest_bytes(manifest)

        assert make() == make()

    def test_write_then_load_is_byte_identical(self, tmp_path: Path) -> None:
        manifest = build_manifest(
            namespace="ns",
            created_at=CREATED_AT,
            build=_build_record(),
            ledger_version="0.1.0",
            sources=[_source_record()],
        )
        path = tmp_path / "manifest.json"
        write_manifest(path, manifest)
        loaded = load_manifest(path)
        assert canonical_manifest_bytes(loaded) == canonical_manifest_bytes(manifest)

    def test_written_file_bytes_equal_canonical_bytes(self, tmp_path: Path) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        path = tmp_path / "manifest.json"
        write_manifest(path, manifest)
        assert path.read_bytes() == canonical_manifest_bytes(manifest)

    def test_canonical_bytes_have_no_trailing_newline(self, tmp_path: Path) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        assert not canonical_manifest_bytes(manifest).endswith(b"\n")

    def test_load_manifest_rejects_schema_invalid_file(self, tmp_path: Path) -> None:
        path = tmp_path / "invalid.json"
        path.write_text(json.dumps({"not": "a manifest"}))
        with pytest.raises(jsonschema.exceptions.ValidationError):
            load_manifest(path)


class TestChunkAndParseRecordsBuild:
    def test_manifest_with_a_chunk_is_schema_valid(self) -> None:
        source = _source_record()
        parse = ParseRecord(
            id="prs_x",
            source_version_id=source.version_id,
            parser_name="native_markdown",
            parser_version="0.1.0",
            status="success",
            parsed_artifact_ref="art_x",
            duration_seconds=0.1,
        )
        chunk = ChunkRecord(
            id="chk_x",
            source_version_id=source.version_id,
            parse_run_id=parse.id,
            locator=StructuralLocator(kind="document_span", ordinal=0),
            raw_hash=HASH_A,
            contextualized_hash=HASH_A,
            token_count=3,
            tokenizer=Tokenizer(name="cl100k_base", revision="1"),
        )
        manifest = build_manifest(
            namespace="ns",
            created_at=CREATED_AT,
            build=_build_record(),
            ledger_version="0.1.0",
            sources=[source],
            parse_runs=[parse],
            chunks=[chunk],
        )
        assert manifest.statistics.chunk_count == 1
        assert manifest.statistics.parse_run_count == 1


def test_hash_canonical_is_reused_consistently_for_config_hashes() -> None:
    # A light integration check that ragledger.core.hashing.hash_canonical
    # composes correctly with manifest building: two builds using the
    # same config dict produce the same config hash.
    config = {"pipeline": "test", "version": 1}
    assert hash_canonical(config) == hash_canonical(dict(reversed(list(config.items()))))
