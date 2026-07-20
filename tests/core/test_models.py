"""Tests for `ragledger.core.models`.

Confirms that every record type can be built and, wrapped in a minimal
manifest, validates against `docs/spec/manifest-v1.schema.json`; that
unknown fields are rejected everywhere (`additionalProperties: false`
parity); that the assertion discriminated union resolves the right
variant class for each of the five `type` values, including when
parsed from raw JSON (as `load_manifest` would); and that naive
(timezone-less) timestamps are rejected, since manifest determinism
depends on every timestamp being an explicit, unambiguous instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ragledger.core.hashing import sha256_hex
from ragledger.core.manifest import build_manifest, validate_manifest_document
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

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
HASH_A = sha256_hex(b"a")
HASH_B = sha256_hex(b"b")


def _build_record() -> BuildRecord:
    return BuildRecord(
        build_id="bld_models_test",
        status="complete",
        source_snapshot_hash=HASH_A,
        pipeline_config_hash=HASH_B,
        started_at=CREATED_AT,
        completed_at=CREATED_AT,
        environment=BuildEnvironment(python_version="3.13.0"),
    )


def _source_record() -> SourceRecord:
    return SourceRecord(
        id="src_x",
        version_id="ver_x",
        namespace="ns",
        uri="file:doc.md",
        media_type="text/markdown",
        size_bytes=10,
        content_hash=HASH_A,
        source_system="local_fs",
        status="active",
    )


def _parse_record() -> ParseRecord:
    return ParseRecord(
        id="prs_x",
        source_version_id="ver_x",
        parser_name="native_markdown",
        parser_version="0.1.0",
        status="success",
        parsed_artifact_ref="art_x",
        duration_seconds=0.1,
    )


def _chunk_record() -> ChunkRecord:
    return ChunkRecord(
        id="chk_x",
        source_version_id="ver_x",
        parse_run_id="prs_x",
        locator=StructuralLocator(kind="document_span", ordinal=0),
        raw_hash=HASH_A,
        contextualized_hash=HASH_A,
        token_count=3,
        tokenizer=Tokenizer(name="cl100k_base", revision="1"),
    )


def _embedding_record() -> EmbeddingRecord:
    return EmbeddingRecord(
        id="emb_x",
        chunk_id="chk_x",
        model=EmbeddingModelInfo(provider="local", name="test", revision="1"),
        dimension=4,
        dtype="float32",
        contextualized_hash=HASH_A,
        generated_at=CREATED_AT,
    )


def _index_binding() -> IndexBinding:
    return IndexBinding(
        id="idx_x",
        target="primary",
        namespace="kb",
        point_id="p-1",
        embedding_id="emb_x",
        expected_payload_hash=HASH_A,
    )


def _artifact_ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="art_x",
        media_type="text/markdown",
        sha256=HASH_A,
        size_bytes=10,
        compression="none",
        encryption="none",
        locator=f"artifacts/{HASH_A}",
        sensitivity="internal",
    )


class TestFullManifestSchemaRoundtrip:
    """Building one of every record type and validating the assembled
    manifest against the schema is the most reliable way to confirm
    every model matches its schema `$defs` entry field-for-field.
    """

    def test_manifest_with_every_record_type_is_schema_valid(self) -> None:
        pii = PiiScanAssertion(
            id="ast_pii",
            subject_ref="chk_x",
            created_at=CREATED_AT,
            scanner=PiiScannerInfo(name="presidio", version="2"),
            status="no_findings_detected",
        )
        license_ = LicenseAssertion(
            id="ast_license",
            subject_ref="ver_x",
            created_at=CREATED_AT,
            spdx_expression="MIT",
            method="user_assertion",
        )
        acl = AclAssertion(
            id="ast_acl",
            subject_ref="ver_x",
            created_at=CREATED_AT,
            acl_hash=HASH_A,
            entries=["PUBLIC"],
        )
        tenant = TenantAssertion(
            id="ast_tenant",
            subject_ref="ver_x",
            created_at=CREATED_AT,
            tenant_hash=HASH_A,
            tenant_key="tenant",
            tenant_value="tenant-a",
        )
        quality = QualityAssertion(
            id="ast_quality",
            subject_ref="prs_x",
            created_at=CREATED_AT,
            warnings=[WarningRecord(code="SHORT_DOCUMENT")],
        )

        manifest = build_manifest(
            namespace="models-test",
            created_at=CREATED_AT,
            build=_build_record(),
            ledger_version="0.1.0",
            sources=[_source_record()],
            parse_runs=[_parse_record()],
            chunks=[_chunk_record()],
            embeddings=[_embedding_record()],
            index_bindings=[_index_binding()],
            assertions=[pii, license_, acl, tenant, quality],
            artifacts=[_artifact_ref()],
        )
        # build_manifest already validates internally; this call proves
        # the model -> dict -> schema path is independently correct too.
        validate_manifest_document(
            manifest.model_dump(mode="json", exclude_none=True, by_alias=True)
        )
        assert manifest.statistics.assertion_count == 5
        assert manifest.statistics.chunk_count == 1


class TestAdditionalPropertiesForbidden:
    def test_source_record_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            SourceRecord.model_validate({**_source_record().model_dump(), "not_a_field": True})

    def test_envelope_rejects_unknown_field(self) -> None:
        manifest = build_manifest(
            namespace="ns", created_at=CREATED_AT, build=_build_record(), ledger_version="0.1.0"
        )
        data = manifest.model_dump(mode="json", exclude_none=True, by_alias=True)
        data["not_a_field"] = True
        with pytest.raises(ValidationError):
            ManifestEnvelope.model_validate(data)

    def test_chunk_metadata_rejects_reserved_key_style_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ChunkRecord.model_validate(
                {**_chunk_record().model_dump(), "metadata": {"not_allowed": "x"}}
            )


class TestAssertionDiscriminatedUnion:
    @pytest.mark.parametrize(
        ("type_value", "extra", "expected_class"),
        [
            (
                "PII_SCAN",
                {"scanner": {"name": "presidio", "version": "2"}, "status": "no_findings_detected"},
                PiiScanAssertion,
            ),
            (
                "LICENSE",
                {"spdx_expression": "MIT", "method": "user_assertion"},
                LicenseAssertion,
            ),
            ("ACL", {"acl_hash": HASH_A, "entries": ["PUBLIC"]}, AclAssertion),
            (
                "TENANT",
                {"tenant_hash": HASH_A, "tenant_key": "t", "tenant_value": "v"},
                TenantAssertion,
            ),
            ("QUALITY", {"warnings": []}, QualityAssertion),
        ],
    )
    def test_manifest_assertions_resolve_the_right_class_from_raw_json(
        self, type_value, extra, expected_class
    ) -> None:
        manifest = build_manifest(
            namespace="ns",
            created_at=CREATED_AT,
            build=_build_record(),
            ledger_version="0.1.0",
            assertions=[
                {
                    "id": "ast_x",
                    "type": type_value,
                    "subject_ref": "ver_x",
                    "created_at": CREATED_AT,
                    **extra,
                }
            ],
        )
        assert isinstance(manifest.assertions[0], expected_class)

        # And the same resolution must work parsing from a plain dict,
        # the shape load_manifest hands to ManifestEnvelope.model_validate.
        raw = manifest.model_dump(mode="json", exclude_none=True, by_alias=True)
        reloaded = ManifestEnvelope.model_validate(raw)
        assert isinstance(reloaded.assertions[0], expected_class)


class TestTimestampsRequireTimezone:
    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BuildRecord(
                build_id="bld_x",
                status="complete",
                source_snapshot_hash=HASH_A,
                pipeline_config_hash=HASH_B,
                started_at=datetime(2026, 1, 1),  # noqa: DTZ001 - intentionally naive
                completed_at=CREATED_AT,
                environment=BuildEnvironment(python_version="3.13.0"),
            )

    def test_non_utc_timezone_is_normalized_to_utc_on_serialization(self) -> None:
        minus_five = timezone(timedelta(hours=-5))
        aware_local = datetime(2026, 1, 1, 0, 0, 0, tzinfo=minus_five)  # 05:00 UTC
        record = _chunk_record()
        embedding = EmbeddingRecord(
            id="emb_y",
            chunk_id=record.id,
            model=EmbeddingModelInfo(provider="local", name="test", revision="1"),
            dimension=4,
            dtype="float32",
            contextualized_hash=HASH_A,
            generated_at=aware_local,
        )
        dumped = embedding.model_dump(mode="json")
        assert dumped["generated_at"] == "2026-01-01T05:00:00Z"

    def test_microseconds_are_preserved_when_present(self) -> None:
        with_micros = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
        record = _chunk_record()
        embedding = EmbeddingRecord(
            id="emb_z",
            chunk_id=record.id,
            model=EmbeddingModelInfo(provider="local", name="test", revision="1"),
            dimension=4,
            dtype="float32",
            contextualized_hash=HASH_A,
            generated_at=with_micros,
        )
        dumped = embedding.model_dump(mode="json")
        assert dumped["generated_at"] == "2026-01-01T00:00:00.123456Z"


class TestUnknownConvention:
    def test_write_status_unknown_is_a_valid_enum_member(self) -> None:
        binding = IndexBinding(
            id="idx_x",
            target="primary",
            namespace="kb",
            point_id="p-1",
            embedding_id="emb_x",
            expected_payload_hash=HASH_A,
            write_status="unknown",
        )
        assert binding.write_status == "unknown"

    def test_discovered_by_unknown_string_is_accepted(self) -> None:
        source = SourceRecord.model_validate(
            {**_source_record().model_dump(), "discovered_by": "unknown"}
        )
        assert source.discovered_by == "unknown"
