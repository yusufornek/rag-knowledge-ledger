"""Tests for `ragledger.connectors.config`: target config models and preflight."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from pydantic import ValidationError

from ragledger.connectors.base import (
    Checkpoint,
    ConnectionTestResult,
    ConnectorCapabilities,
    ConnectorConfigError,
    ConsistencyInfo,
    ConsistencyMode,
    NormalizedPoint,
    SnapshotCompleteness,
    TargetSchema,
    VectorFieldSchema,
    VectorTargetConnector,
)
from ragledger.connectors.config import (
    PgvectorTargetConfig,
    QdrantTargetConfig,
    resolve_env_credential,
    run_preflight,
)

# --------------------------------------------------------------------------
# resolve_env_credential
# --------------------------------------------------------------------------


def test_resolve_env_credential_reads_from_supplied_mapping() -> None:
    assert resolve_env_credential("QDRANT_API_KEY", env={"QDRANT_API_KEY": "secret"}) == "secret"


def test_resolve_env_credential_raises_when_unset() -> None:
    with pytest.raises(ConnectorConfigError):
        resolve_env_credential("QDRANT_API_KEY", env={})


def test_resolve_env_credential_rejects_invalid_variable_name() -> None:
    with pytest.raises(ConnectorConfigError):
        resolve_env_credential("not a valid name", env={})


# --------------------------------------------------------------------------
# QdrantTargetConfig
# --------------------------------------------------------------------------


def _qdrant_config(**overrides: object) -> QdrantTargetConfig:
    fields: dict[str, object] = {
        "endpoint": "https://qdrant.example.com",
        "collection": "support_kb",
        "api_key_env": "QDRANT_API_KEY",
        "vector_name": "dense",
        "payload_mapping": {
            "source_id": "ragledger.source_id",
            "chunk_id": "ragledger.chunk_id",
            "tenant": "tenant_id",
            "acl": "allowed_groups",
        },
    }
    fields.update(overrides)
    return QdrantTargetConfig.model_validate(fields)


def test_qdrant_config_parses_section_35_1_example() -> None:
    config = _qdrant_config()
    assert config.endpoint == "https://qdrant.example.com"
    assert config.collection == "support_kb"
    assert config.payload_mapping.source_id == "ragledger.source_id"
    assert config.snapshot.include_vectors is False
    assert config.snapshot.page_size == 256


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://qdrant.example.com",
        "https://user:pass@qdrant.example.com",
        "https://qdrant.example.com?api_key=abc",
        "https://",
        "not-a-url",
    ],
)
def test_qdrant_config_rejects_unsafe_endpoint(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        _qdrant_config(endpoint=endpoint)


def test_qdrant_config_normalizes_trailing_slash() -> None:
    config = _qdrant_config(endpoint="https://qdrant.example.com/")
    assert config.endpoint == "https://qdrant.example.com"


@pytest.mark.parametrize("collection", ["", "a" * 256, "bad name", "bad/name", "bad;drop"])
def test_qdrant_config_rejects_invalid_collection_name(collection: str) -> None:
    with pytest.raises(ValidationError):
        _qdrant_config(collection=collection)


def test_qdrant_config_rejects_invalid_api_key_env() -> None:
    with pytest.raises(ValidationError):
        _qdrant_config(api_key_env="not valid")


@pytest.mark.parametrize("path", ["", ".leading", "trailing.", "a..b", "a.b c"])
def test_qdrant_config_rejects_invalid_payload_mapping_path(path: str) -> None:
    with pytest.raises(ValidationError):
        _qdrant_config(payload_mapping={"source_id": path})


def test_qdrant_config_resolve_api_key() -> None:
    config = _qdrant_config()
    assert config.resolve_api_key(env={"QDRANT_API_KEY": "s3cr3t"}) == "s3cr3t"


def test_qdrant_config_resolve_api_key_none_when_not_configured() -> None:
    config = _qdrant_config(api_key_env=None)
    assert config.resolve_api_key(env={}) is None


# --------------------------------------------------------------------------
# PgvectorTargetConfig
# --------------------------------------------------------------------------


def _pgvector_config(**overrides: object) -> PgvectorTargetConfig:
    fields: dict[str, object] = {
        "dsn_env": "RAG_DB_DSN",
        "schema": "public",
        "table": "document_chunks",
        "primary_key": ["id"],
        "vector_column": "embedding",
        "mapping": {
            "source_id": "source_id",
            "chunk_id": "chunk_id",
            "tenant": "tenant_id",
            "acl": "acl_json",
        },
        "where": {"tenant_id": "acme"},
    }
    fields.update(overrides)
    return PgvectorTargetConfig.model_validate(fields)


def test_pgvector_config_parses_section_35_2_example() -> None:
    config = _pgvector_config()
    assert config.schema_name == "public"
    assert config.table == "document_chunks"
    assert config.primary_key == ["id"]
    assert config.where == {"tenant_id": "acme"}
    assert config.consistency == "repeatable_read"


def test_pgvector_config_accepts_composite_primary_key() -> None:
    config = _pgvector_config(primary_key=["tenant_id", "chunk_id"])
    assert config.primary_key == ["tenant_id", "chunk_id"]


@pytest.mark.parametrize("identifier", ["", "bad name", "bad;drop table", "1leading_digit"])
def test_pgvector_config_rejects_invalid_table_identifier(identifier: str) -> None:
    with pytest.raises(ValidationError):
        _pgvector_config(table=identifier)


def test_pgvector_config_rejects_empty_primary_key() -> None:
    with pytest.raises(ValidationError):
        _pgvector_config(primary_key=[])


def test_pgvector_config_rejects_vector_column_as_primary_key() -> None:
    with pytest.raises(ValidationError):
        _pgvector_config(primary_key=["embedding"], vector_column="embedding")


def test_pgvector_config_rejects_invalid_where_column() -> None:
    with pytest.raises(ValidationError):
        _pgvector_config(where={"bad column; drop": "acme"})


def test_pgvector_config_rejects_empty_where_list_value() -> None:
    with pytest.raises(ValidationError):
        _pgvector_config(where={"tenant_id": []})


def test_pgvector_config_accepts_where_list_value() -> None:
    config = _pgvector_config(where={"tenant_id": ["acme", "beta"]})
    assert config.where == {"tenant_id": ["acme", "beta"]}


def test_pgvector_config_resolve_dsn() -> None:
    config = _pgvector_config()
    dsn = config.resolve_dsn(env={"RAG_DB_DSN": "postgresql://x/y"})
    assert dsn == "postgresql://x/y"


# --------------------------------------------------------------------------
# run_preflight
# --------------------------------------------------------------------------


class _StubConnector(VectorTargetConnector[object]):
    """A minimal connector double for exercising `run_preflight` in isolation."""

    def __init__(
        self,
        *,
        connection_ok: bool = True,
        dimension: int | None = 768,
        vector_name: str | None = "dense",
    ) -> None:
        self._connection_ok = connection_ok
        self._dimension = dimension
        self._vector_name = vector_name

    def validate_configuration(self) -> None:
        return None

    def test_connection(self) -> ConnectionTestResult:
        if not self._connection_ok:
            return ConnectionTestResult(ok=False, message="unreachable")
        return ConnectionTestResult(ok=True, message="reachable")

    def inspect_target_schema(self) -> TargetSchema:
        vector_fields = ()
        if self._dimension is not None:
            vector_fields = (
                VectorFieldSchema(name=self._vector_name or "", dimension=self._dimension),
            )
        return TargetSchema(
            target_id="kb",
            scope="kb",
            vector_fields=vector_fields,
            point_id_type="string",
        )

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target_type="stub",
            supports_resume=True,
            supports_vector_hash=True,
            supports_sampling=False,
            max_page_size=None,
            consistency_modes=(ConsistencyMode.BEST_EFFORT_LIVE,),
        )

    def estimate_count(self) -> int | None:
        return None

    def normalize_point(self, raw: object, *, include_vectors: bool = False) -> NormalizedPoint:
        raise NotImplementedError

    def iterate_points(
        self,
        *,
        checkpoint: Checkpoint | None = None,
        projection: Sequence[str] | None = None,
        include_vectors: bool = False,
    ) -> Iterator[NormalizedPoint]:
        return iter(())

    def get_consistency_info(self) -> ConsistencyInfo:
        return ConsistencyInfo(
            mode=ConsistencyMode.BEST_EFFORT_LIVE,
            completeness=SnapshotCompleteness.COMPLETE,
            start_count=0,
            end_count=0,
            observed_count=0,
        )

    def close(self) -> None:
        return None


def test_run_preflight_reports_unreachable_target() -> None:
    result = run_preflight(_StubConnector(connection_ok=False))
    assert result.reachable is False
    assert result.auth_ok is False
    assert result.schema is None


def test_run_preflight_reports_dimension_match() -> None:
    result = run_preflight(
        _StubConnector(dimension=768), expected_dimension=768, vector_name="dense"
    )
    assert result.reachable is True
    assert result.dimension_match is True
    assert result.observed_dimension == 768


def test_run_preflight_reports_dimension_mismatch() -> None:
    result = run_preflight(
        _StubConnector(dimension=512), expected_dimension=768, vector_name="dense"
    )
    assert result.dimension_match is False
    assert "mismatch" in result.message


def test_run_preflight_without_expected_dimension_skips_comparison() -> None:
    result = run_preflight(_StubConnector(dimension=768))
    assert result.dimension_match is None
    assert result.expected_dimension is None
