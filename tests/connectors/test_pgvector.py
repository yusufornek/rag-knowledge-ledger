"""Tests for `ragledger.connectors.pgvector`, using a fake psycopg-shaped connection.

No real database is used: `_FakeConnection`/`_FakeCursor` implement
only the slice of the psycopg surface `PgvectorConnector` actually
calls, driven by a small in-memory `_FakeTable`. Covers streaming via
`fetchmany`, primary-key ordering, checkpoint/resume, read-only
transaction configuration, consistency-mode behavior, and the section
42.2 statement whitelist.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from psycopg import sql

from ragledger.connectors.base import (
    ConnectorConfigError,
    ConnectorConnectionError,
    ConnectorMutationBlockedError,
    SnapshotCompleteness,
)
from ragledger.connectors.config import PgvectorTargetConfig
from ragledger.connectors.pgvector import (
    PgvectorConnector,
    _assert_read_only_statement,
    _coerce_acl_value,
    _coerce_scalar,
    _GuardedCursor,
    _parse_pgvector_literal,
)

SCHEMA = "public"
TABLE = "document_chunks"


def _config(**overrides: object) -> PgvectorTargetConfig:
    fields: dict[str, object] = {
        "dsn_env": "RAG_DB_DSN",
        "schema": SCHEMA,
        "table": TABLE,
        "primary_key": ["id"],
        "vector_column": "embedding",
        "mapping": {
            "source_id": "source_id",
            "chunk_id": "chunk_id",
            "tenant": "tenant_id",
            "acl": "acl_json",
        },
        "fetch_size": 2,
    }
    fields.update(overrides)
    return PgvectorTargetConfig.model_validate(fields)


class _FakeTable:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        primary_key: Sequence[str] = ("id",),
        vector_dimension: int | None = 3,
        index_names: tuple[str, ...] = ("document_chunks_pkey",),
        exists: bool = True,
        has_select: bool = True,
        count_override: list[int] | None = None,
    ) -> None:
        self.rows = rows
        self.primary_key = tuple(primary_key)
        self.vector_dimension = vector_dimension
        self.index_names = index_names
        self.exists = exists
        self.has_select = has_select
        self.count_override = count_override
        self.fetchmany_sizes: list[int] = []
        # Set by `_connector()` from the matching `PgvectorTargetConfig.where`,
        # so the fake's WHERE-clause simulation and the connector's real
        # `_where_fragments()` param order always agree.
        self.where: dict[str, Any] = {}


class _FakeCursor:
    def __init__(self, table: _FakeTable) -> None:
        self._table = table
        self._rows: list[dict[str, Any]] = []
        self._pos = 0
        self.executed: list[str] = []

    def execute(self, query: Any, params: Sequence[Any] | None = None) -> Any:
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.executed.append(text)
        params = list(params) if params else []
        self._rows = self._resolve(text, params)
        self._pos = 0
        return self

    def _resolve(self, text: str, params: list[Any]) -> list[dict[str, Any]]:
        table = self._table
        if "atttypmod" in text:
            if table.vector_dimension is None:
                return []
            return [{"atttypmod": table.vector_dimension, "typname": "vector"}]
        if "pg_index" in text:
            return [{"index_name": name} for name in table.index_names]
        if "to_regclass" in text:
            relation = f"{SCHEMA}.{TABLE}" if table.exists else None
            return [{"relation": relation, "has_select": table.has_select}]
        if "count(*)" in text:
            if table.count_override is not None:
                value = (
                    table.count_override.pop(0)
                    if len(table.count_override) > 1
                    else table.count_override[0]
                )
                return [{"row_count": value}]
            filtered, _ = self._apply_where(table.rows, params)
            return [{"row_count": len(filtered)}]
        return self._select(params)

    def _apply_where(
        self, rows: list[dict[str, Any]], params: list[Any]
    ) -> tuple[list[dict[str, Any]], int]:
        """Replay `config.where` filtering against ``params``' leading values.

        This never parses the rendered SQL text for the WHERE clause:
        it trusts that `PgvectorConnector._where_fragments` binds
        exactly one parameter per `table.where` entry, in that dict's
        iteration order -- the same invariant the production code
        implements -- and consumes ``params`` positionally on that
        basis.
        """
        filtered = rows
        index = 0
        for column, value in self._table.where.items():
            bound = params[index]
            index += 1
            if isinstance(value, list):
                filtered = [row for row in filtered if row.get(column) in bound]
            else:
                filtered = [row for row in filtered if row.get(column) == bound]
        return filtered, index

    def _select(self, params: list[Any]) -> list[dict[str, Any]]:
        table = self._table
        rows, where_param_count = self._apply_where(table.rows, params)
        checkpoint_params = params[where_param_count:]
        if checkpoint_params:
            cursor_tuple = tuple(checkpoint_params)
            rows = [
                row for row in rows if tuple(row[col] for col in table.primary_key) > cursor_tuple
            ]
        rows.sort(key=lambda row: tuple(row[col] for col in table.primary_key))
        return rows

    def fetchone(self) -> dict[str, Any] | None:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        self._table.fetchmany_sizes.append(size)
        batch = self._rows[self._pos : self._pos + size]
        self._pos += len(batch)
        return batch

    def fetchall(self) -> list[dict[str, Any]]:
        batch = self._rows[self._pos :]
        self._pos = len(self._rows)
        return batch

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, table: _FakeTable) -> None:
        self.table = table
        self.read_only: bool | None = None
        self.isolation_level: Any = None
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, name: str = "", *, row_factory: Any = None) -> _FakeCursor:
        return _FakeCursor(self.table)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _rows(*ids: int) -> list[dict[str, Any]]:
    return [
        {
            "id": i,
            "source_id": f"src_{i}",
            "chunk_id": f"chk_{i}",
            "tenant_id": "acme",
            "acl_json": ["group:support"],
            "embedding": "[0.1,0.2,0.3]",
        }
        for i in ids
    ]


def _connector(
    table: _FakeTable, config: PgvectorTargetConfig | None = None
) -> tuple[PgvectorConnector, _FakeConnection]:
    cfg = config or _config()
    table.where = dict(cfg.where)
    connection = _FakeConnection(table)
    connector = PgvectorConnector(cfg, connection=connection)
    return connector, connection


# --------------------------------------------------------------------------
# Streaming, ordering, resume
# --------------------------------------------------------------------------


def test_iterate_points_streams_in_fetch_size_batches() -> None:
    table = _FakeTable(_rows(3, 1, 2), count_override=None)  # inserted out of order
    connector, _ = _connector(table)
    points = list(connector.iterate_points())

    assert [point.point_id for point in points] == [1, 2, 3]
    assert table.fetchmany_sizes and all(size == 2 for size in table.fetchmany_sizes)
    connector.close()


def test_iterate_points_orders_by_primary_key() -> None:
    table = _FakeTable(_rows(5, 2, 4, 1, 3))
    connector, _ = _connector(table)
    points = list(connector.iterate_points())
    assert [point.point_id for point in points] == [1, 2, 3, 4, 5]
    connector.close()


def test_iterate_points_resumes_from_checkpoint() -> None:
    table = _FakeTable(_rows(1, 2, 3, 4))
    connector, _ = _connector(table)
    resumed = list(connector.iterate_points(checkpoint={"id": 2}))
    assert [point.point_id for point in resumed] == [3, 4]
    connector.close()


def test_iterate_points_rejects_non_object_checkpoint() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    with pytest.raises(Exception, match="checkpoint"):
        list(connector.iterate_points(checkpoint="not-an-object"))
    connector.close()


def test_iterate_points_normalizes_mapped_fields() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    points = list(connector.iterate_points())
    assert points[0].source_id == "src_1"
    assert points[0].chunk_id == "chk_1"
    assert points[0].tenant == "acme"
    assert points[0].acl == ["group:support"]
    connector.close()


def test_iterate_points_applies_projection() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    points = list(connector.iterate_points(projection=["source_id"]))
    assert points[0].payload_projection == {"source_id": "src_1"}
    assert points[0].chunk_id is None
    connector.close()


def test_iterate_points_applies_row_level_where_filter() -> None:
    rows = _rows(1, 2, 3)
    rows[1]["tenant_id"] = "other-tenant"
    table = _FakeTable(rows)
    config = _config(where={"tenant_id": "acme"})
    connector, _ = _connector(table, config)
    points = list(connector.iterate_points())
    assert [point.point_id for point in points] == [1, 3]
    connector.close()


def test_composite_primary_key_produces_object_point_id() -> None:
    table = _FakeTable(
        [
            {"tenant_id": "acme", "chunk_id": "c1", "source_id": "s1", "acl_json": None},
        ],
        primary_key=("tenant_id", "chunk_id"),
    )
    config = _config(primary_key=["tenant_id", "chunk_id"])
    connector, _ = _connector(table, config)
    points = list(connector.iterate_points())
    assert points[0].point_id == {"tenant_id": "acme", "chunk_id": "c1"}
    connector.close()


def test_normalize_point_warns_on_missing_mapped_field() -> None:
    table = _FakeTable([{"id": 1, "source_id": None, "chunk_id": None, "tenant_id": None}])
    connector, _ = _connector(table)
    points = list(connector.iterate_points())
    assert any(w.startswith("missing_mapped_field:") for w in points[0].normalization_warnings)
    connector.close()


def test_include_vectors_hashes_pgvector_text_literal() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    points = list(connector.iterate_points(include_vectors=True))
    assert points[0].vector_hashes is not None
    assert "embedding" in points[0].vector_hashes
    connector.close()


# --------------------------------------------------------------------------
# Read-only transaction configuration
# --------------------------------------------------------------------------


def test_connect_configures_read_only_repeatable_read_by_default() -> None:
    table = _FakeTable(_rows(1))
    connector, connection = _connector(table)
    list(connector.iterate_points())
    assert connection.read_only is True
    assert connection.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
    connector.close()


def test_best_effort_paged_does_not_force_repeatable_read() -> None:
    table = _FakeTable(_rows(1))
    config = _config(consistency="best_effort_paged")
    connector, connection = _connector(table, config)
    list(connector.iterate_points())
    assert connection.read_only is True
    assert connection.isolation_level is None
    connector.close()


def test_connection_factory_failure_is_reported_gracefully() -> None:
    def failing_factory() -> Any:
        raise psycopg.OperationalError("password authentication failed")

    connector = PgvectorConnector(_config(), connection_factory=failing_factory)
    result = connector.test_connection()
    assert result.ok is False
    assert "failed" in result.message.lower()
    connector.close()


def test_connection_factory_failure_raises_on_iterate() -> None:
    def failing_factory() -> Any:
        raise psycopg.OperationalError("password authentication failed")

    connector = PgvectorConnector(_config(), connection_factory=failing_factory)
    with pytest.raises(ConnectorConnectionError):
        list(connector.iterate_points())


# --------------------------------------------------------------------------
# test_connection / inspect_target_schema
# --------------------------------------------------------------------------


def test_test_connection_reports_missing_table() -> None:
    table = _FakeTable(_rows(1), exists=False)
    connector, _ = _connector(table)
    result = connector.test_connection()
    assert result.ok is False
    assert "not found" in result.message
    connector.close()


def test_test_connection_reports_missing_privilege() -> None:
    table = _FakeTable(_rows(1), has_select=False)
    connector, _ = _connector(table)
    result = connector.test_connection()
    assert result.ok is False
    assert "privilege" in result.message
    connector.close()


def test_inspect_target_schema_reports_dimension_and_indexes() -> None:
    table = _FakeTable(_rows(1), vector_dimension=768, index_names=("idx_a", "idx_b"))
    connector, _ = _connector(table)
    schema = connector.inspect_target_schema()
    assert schema.vector_fields[0].dimension == 768
    assert schema.extra["indexes"] == ("idx_a", "idx_b")
    connector.close()


def test_inspect_target_schema_handles_unbounded_vector_column() -> None:
    table = _FakeTable(_rows(1), vector_dimension=None)
    connector, _ = _connector(table)
    schema = connector.inspect_target_schema()
    assert schema.vector_fields == ()
    connector.close()


# --------------------------------------------------------------------------
# Consistency
# --------------------------------------------------------------------------


def test_repeatable_read_consistency_is_always_complete() -> None:
    table = _FakeTable(_rows(1, 2, 3))
    connector, _ = _connector(table)
    list(connector.iterate_points())
    consistency = connector.get_consistency_info()
    assert consistency.completeness is SnapshotCompleteness.COMPLETE
    assert consistency.start_count == consistency.end_count == 3
    connector.close()


def test_best_effort_paged_marks_incomplete_on_drift() -> None:
    # Three count(*) calls happen in this pass: one from the schema-cache
    # priming `inspect_target_schema()` call (its own approx_point_count
    # is not used for consistency), then the pass's own start_count and
    # end_count probes -- the first override value is a throwaway.
    table = _FakeTable(_rows(1, 2), count_override=[999, 10, 8])
    config = _config(consistency="best_effort_paged")
    connector, _ = _connector(table, config)
    list(connector.iterate_points())
    consistency = connector.get_consistency_info()
    assert consistency.completeness is SnapshotCompleteness.INCOMPLETE
    assert consistency.start_count == 10
    assert consistency.end_count == 8
    connector.close()


def test_get_consistency_info_before_iteration_raises() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    with pytest.raises(RuntimeError):
        connector.get_consistency_info()
    connector.close()


# --------------------------------------------------------------------------
# Section 42.2 mutation guard: statement whitelist
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO document_chunks VALUES (1)",
        "UPDATE document_chunks SET x = 1",
        "DELETE FROM document_chunks",
        "DROP TABLE document_chunks",
        "ALTER TABLE document_chunks ADD COLUMN x int",
        "TRUNCATE document_chunks",
        "CREATE TABLE evil (id int)",
        "GRANT ALL ON document_chunks TO public",
        "COPY document_chunks FROM STDIN",
    ],
)
def test_assert_read_only_statement_blocks_mutations(statement: str) -> None:
    with pytest.raises(ConnectorMutationBlockedError):
        _assert_read_only_statement(statement)


@pytest.mark.parametrize(
    "statement",
    ["SELECT 1", "select * from document_chunks", "SHOW statement_timeout"],
)
def test_assert_read_only_statement_allows_reads(statement: str) -> None:
    _assert_read_only_statement(statement)  # must not raise


def test_guarded_cursor_blocks_before_reaching_underlying_cursor() -> None:
    table = _FakeTable(_rows(1))
    underlying = _FakeCursor(table)
    guarded = _GuardedCursor(underlying, connection=None)

    with pytest.raises(ConnectorMutationBlockedError):
        guarded.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(TABLE)))

    assert underlying.executed == []


def test_guarded_cursor_allows_select() -> None:
    table = _FakeTable(_rows(1))
    underlying = _FakeCursor(table)
    guarded = _GuardedCursor(underlying, connection=None)

    guarded.execute(
        sql.SQL("SELECT {} FROM {}").format(sql.Identifier("id"), sql.Identifier(TABLE))
    )
    assert underlying.executed


# --------------------------------------------------------------------------
# Raw record helper functions
# --------------------------------------------------------------------------


def test_coerce_scalar_passes_primitives_through() -> None:
    assert _coerce_scalar(None) is None
    assert _coerce_scalar(42) == 42
    assert _coerce_scalar("x") == "x"
    assert _coerce_scalar(True) is True


def test_coerce_scalar_stringifies_non_primitive() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque-value"

    assert _coerce_scalar(Opaque()) == "opaque-value"


def test_coerce_acl_value_accepts_list() -> None:
    warnings: list[str] = []
    assert _coerce_acl_value(["a", "b"], warnings) == ["a", "b"]
    assert warnings == []


def test_coerce_acl_value_wraps_bare_string() -> None:
    warnings: list[str] = []
    assert _coerce_acl_value("solo", warnings) == ["solo"]
    assert warnings == []


def test_coerce_acl_value_warns_on_other_types() -> None:
    warnings: list[str] = []
    assert _coerce_acl_value(42, warnings) == ["42"]
    assert warnings == ["acl_not_list"]


def test_parse_pgvector_literal_from_text() -> None:
    assert _parse_pgvector_literal("[0.1,0.2,0.3]") == [0.1, 0.2, 0.3]


def test_parse_pgvector_literal_empty_text_is_empty_vector() -> None:
    assert _parse_pgvector_literal("[]") == []


def test_parse_pgvector_literal_malformed_text_returns_none() -> None:
    assert _parse_pgvector_literal("[not,a,number]") is None


def test_parse_pgvector_literal_from_list() -> None:
    assert _parse_pgvector_literal([1, 2, 3]) == [1.0, 2.0, 3.0]


def test_parse_pgvector_literal_unrecognized_shape_returns_none() -> None:
    assert _parse_pgvector_literal({"not": "a vector"}) is None


# --------------------------------------------------------------------------
# Additional connector coverage
# --------------------------------------------------------------------------


def test_validate_configuration_rejects_mutated_invalid_config() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    connector._config.table = "not a valid identifier"  # type: ignore[misc]
    with pytest.raises(ConnectorConfigError):
        connector.validate_configuration()
    connector.close()


def test_test_connection_succeeds() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    result = connector.test_connection()
    assert result.ok is True
    assert result.latency_ms is not None
    connector.close()


def test_capabilities_reports_pgvector_target_type() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    capabilities = connector.capabilities()
    assert capabilities.target_type == "pgvector"
    assert capabilities.max_page_size == connector._config.fetch_size
    connector.close()


def test_estimate_count_uses_schema_approx_count() -> None:
    table = _FakeTable(_rows(1, 2, 3))
    connector, _ = _connector(table)
    assert connector.estimate_count() == 3
    connector.close()


def test_normalize_point_warns_when_vector_missing() -> None:
    table = _FakeTable([{"id": 1, "source_id": "s1"}])
    connector, _ = _connector(table)
    connector.inspect_target_schema()
    point = connector.normalize_point({"id": 1}, include_vectors=True)
    assert "vector_missing" in point.normalization_warnings
    connector.close()


def test_normalize_point_warns_on_unrecognized_vector_shape() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    connector.inspect_target_schema()
    raw = {"id": 1, "embedding": {"nested": True}}
    point = connector.normalize_point(raw, include_vectors=True)
    assert "vector_shape_unrecognized" in point.normalization_warnings
    connector.close()


def test_where_list_value_generates_any_predicate() -> None:
    rows = _rows(1, 2, 3)
    rows[2]["tenant_id"] = "other"
    table = _FakeTable(rows)
    config = _config(where={"tenant_id": ["acme", "beta"]})
    connector, _ = _connector(table, config)
    points = list(connector.iterate_points())
    assert [point.point_id for point in points] == [1, 2]
    connector.close()


def test_build_query_rejects_checkpoint_missing_primary_key_column() -> None:
    table = _FakeTable(_rows(1))
    connector, _ = _connector(table)
    with pytest.raises(ConnectorConfigError, match="primary key column"):
        list(connector.iterate_points(checkpoint={"not_id": 1}))
    connector.close()


def test_fetch_vector_dimension_handles_non_positive_atttypmod() -> None:
    table = _FakeTable(_rows(1), vector_dimension=-1)  # unconstrained vector column
    connector, _ = _connector(table)
    schema = connector.inspect_target_schema()
    assert schema.vector_fields == ()
    connector.close()
