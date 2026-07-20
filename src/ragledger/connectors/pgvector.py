"""pgvector connector, per PROJECT_SPEC.md sections 8.12, 13.4, and 35.2.

Talks to PostgreSQL through `psycopg` (psycopg3) directly; there is no
SQLAlchemy dependency available to this package, so where section 35.1
speaks of "SQLAlchemy quoted identifiers" this module uses the
equivalent psycopg primitive, `psycopg.sql.Identifier`/`psycopg.sql.SQL`,
for every schema/table/column name a query is built from -- table,
column, and `where`-clause names are already restricted to a bounded
safe-identifier pattern at config-validation time
(`ragledger.connectors.config`), and are always passed through
`sql.Identifier` rather than string-interpolated, so there is no path
by which a configured identifier (let alone a value) becomes a raw SQL
fragment (FR-111).

Every statement this connector issues -- including its own catalog
introspection queries -- is executed through `_GuardedCursor`, whose
`execute` checks the rendered SQL text against `_assert_read_only_statement`
before it ever reaches the server: only `SELECT`/`SHOW` are permitted.
This is the section 42.2 statement whitelist. It sits on top of, not
instead of, the connection-level read-only transaction
(`connection.read_only = True`, set in `_configure_connection` before
any transaction begins) -- two independent layers, either of which
alone would already stop a mutating statement from taking effect.

Consistency (section 13.4): the default `consistency: repeatable_read`
mode opens one `REPEATABLE READ`, read-only transaction per
`iterate_points` pass; the row-count probe and the row stream itself
therefore share one database snapshot, so no drift is possible within
a pass by construction (`ConsistencyMode.STRICT_CONSISTENT`). The
`best_effort_paged` mode leaves the transaction at the default
isolation level and instead re-checks the row count after the stream
completes, reporting `SnapshotCompleteness.INCOMPLETE` on a mismatch
-- the same detect-rather-than-prevent approach the Qdrant connector
uses, offered for cases where holding one long-lived `REPEATABLE READ`
transaction open for a very large table is operationally undesirable
(section 13.4: "Uzun transaction riskli").
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ragledger.connectors.base import (
    Checkpoint,
    ConnectionTestResult,
    ConnectorCapabilities,
    ConnectorConfigError,
    ConnectorConnectionError,
    ConnectorMutationBlockedError,
    ConsistencyInfo,
    ConsistencyMode,
    NormalizedPoint,
    SnapshotCompleteness,
    TargetSchema,
    VectorFieldSchema,
    VectorTargetConnector,
    apply_projection,
    compute_payload_hash,
    hash_vector,
)
from ragledger.connectors.config import PgvectorTargetConfig
from ragledger.core.canonical import canonical_bytes
from ragledger.core.models import PointId

__all__ = ["PgvectorConnector"]

# --------------------------------------------------------------------------
# Section 42.2 mutation guard: statement whitelist
# --------------------------------------------------------------------------

_ALLOWED_STATEMENT_RE = re.compile(r"^\s*(SELECT|SHOW)\b", re.IGNORECASE)


def _assert_read_only_statement(query_text: str) -> None:
    """Raise unless ``query_text`` begins with SELECT or SHOW.

    An allowlist, not a denylist: anything that is not recognizably a
    read query -- INSERT, UPDATE, DELETE, every DDL statement, COPY,
    CALL, and so on -- is rejected by default, with no attempt made to
    enumerate every dangerous keyword.
    """
    if not _ALLOWED_STATEMENT_RE.match(query_text):
        raise ConnectorMutationBlockedError(
            f"blocked non-read SQL statement: {query_text.strip()[:120]!r}"
        )


class _GuardedCursor:
    """Wraps a psycopg (or fake, in tests) cursor to enforce the statement whitelist.

    Every other cursor method (`fetchmany`, `fetchone`, `fetchall`,
    `description`, iteration) passes straight through via
    `__getattr__`/`__iter__`; only `execute` is intercepted. ``connection``
    is kept only to pair a cursor with the connection it belongs to for
    callers that need that association; rendering the query text for
    the whitelist check always uses `as_string(None)` -- every
    identifier this module ever composes a query from is already
    restricted to a plain-ASCII safe pattern at config-validation time
    (`ragledger.connectors.config`), so no connection-specific encoding
    context is ever needed to render it correctly.
    """

    def __init__(self, cursor: Any, connection: Any) -> None:
        self._cursor = cursor
        self._connection = connection

    def execute(self, query: Any, params: Sequence[Any] | None = None) -> Any:
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        _assert_read_only_statement(text)
        return self._cursor.execute(query, params)

    def close(self) -> None:
        self._cursor.close()

    def __iter__(self) -> Any:
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


# --------------------------------------------------------------------------
# Raw record helpers
# --------------------------------------------------------------------------


def _coerce_scalar(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _coerce_acl_value(value: Any, warnings: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    warnings.append("acl_not_list")
    return [str(value)]


def _parse_pgvector_literal(value: Any) -> list[float] | None:
    """Parse a pgvector column value into a list of floats.

    No `pgvector` Python package is available to this connector (only
    `psycopg[binary]`), so PostgreSQL's text representation of the
    `vector` type -- ``"[0.1,0.2,0.3]"`` -- is parsed by hand here
    rather than relying on a registered type adapter.
    """
    if isinstance(value, list):
        try:
            return [float(item) for item in value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        if not text:
            return []
        try:
            return [float(item) for item in text.split(",")]
        except ValueError:
            return None
    return None


def _point_id_text(point_id: PointId) -> str:
    if isinstance(point_id, dict | list):
        return canonical_bytes(point_id).decode("utf-8")
    return str(point_id)


# --------------------------------------------------------------------------
# Connector
# --------------------------------------------------------------------------


class PgvectorConnector(VectorTargetConnector[dict[str, Any]]):
    """A read-only pgvector connector, per PROJECT_SPEC.md section 8.12.

    For production use, construct with just ``config`` (and optionally
    ``env``): a real `psycopg.Connection` is opened lazily, on first
    use, from `config.resolve_dsn`. Tests instead pass
    ``connection_factory`` (or a ready-made ``connection``) with a
    fake object implementing only the small slice of the psycopg
    surface this module actually calls -- see `tests/connectors/test_pgvector.py`.
    """

    def __init__(
        self,
        config: PgvectorTargetConfig,
        *,
        env: Mapping[str, str] | None = None,
        connection: Any | None = None,
        connection_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._env = env
        self._connection_factory = connection_factory
        self._connection: Any | None = None
        self._clock = clock
        self._schema_cache: TargetSchema | None = None
        self._consistency: ConsistencyInfo | None = None
        if connection is not None:
            self._connection = connection
            self._configure_connection(connection)

    # -- interface ---------------------------------------------------

    def validate_configuration(self) -> None:
        try:
            type(self._config).model_validate(self._config.model_dump(by_alias=True))
        except Exception as exc:  # pydantic.ValidationError, defensively broad
            raise ConnectorConfigError(f"invalid pgvector target configuration: {exc}") from exc

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        try:
            connection = self._ensure_connection()
        except ConnectorConnectionError as exc:
            return ConnectionTestResult(ok=False, message=str(exc))
        qualified = f"{self._config.schema_name}.{self._config.table}"
        try:
            connection.rollback()
            cursor = _GuardedCursor(connection.cursor(row_factory=dict_row), connection)
            try:
                cursor.execute(
                    sql.SQL(
                        "SELECT to_regclass(%s) AS relation, "
                        "has_table_privilege(%s, 'SELECT') AS has_select"
                    ),
                    [qualified, qualified],
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            connection.commit()
        except ConnectorMutationBlockedError:
            raise
        except Exception as exc:  # psycopg.Error and friends
            return ConnectionTestResult(ok=False, message=f"connection check failed: {exc}")
        latency_ms = (time.monotonic() - started) * 1000
        if row is None or row.get("relation") is None:
            return ConnectionTestResult(
                ok=False, message=f"table {qualified!r} not found", latency_ms=latency_ms
            )
        if not row.get("has_select"):
            return ConnectionTestResult(
                ok=False,
                message=f"missing SELECT privilege on {qualified!r}",
                latency_ms=latency_ms,
            )
        return ConnectionTestResult(ok=True, message="reachable", latency_ms=latency_ms)

    def inspect_target_schema(self) -> TargetSchema:
        connection = self._ensure_connection()
        connection.rollback()
        dimension = self._fetch_vector_dimension(connection)
        index_names = self._fetch_index_names(connection)
        approx_count = self._count_rows(connection)
        connection.commit()
        scope = f"{self._config.schema_name}.{self._config.table}"
        vector_fields: tuple[VectorFieldSchema, ...] = ()
        if dimension is not None:
            vector_fields = (
                VectorFieldSchema(name=self._config.vector_column, dimension=dimension),
            )
        schema = TargetSchema(
            target_id=scope,
            scope=scope,
            vector_fields=vector_fields,
            point_id_type="composite" if len(self._config.primary_key) > 1 else "scalar",
            payload_indexes=(),
            approx_point_count=approx_count,
            resolved_scope=None,
            extra={"indexes": index_names} if index_names else {},
        )
        self._schema_cache = schema
        return schema

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target_type="pgvector",
            supports_resume=True,
            supports_vector_hash=True,
            supports_sampling=False,
            max_page_size=self._config.fetch_size,
            consistency_modes=(
                ConsistencyMode.STRICT_CONSISTENT,
                ConsistencyMode.BEST_EFFORT_PAGED,
            ),
        )

    def estimate_count(self) -> int | None:
        schema = self._schema_cache or self.inspect_target_schema()
        return schema.approx_point_count

    def normalize_point(
        self, raw: dict[str, Any], *, include_vectors: bool = False
    ) -> NormalizedPoint:
        warnings: list[str] = []
        pk_cols = self._config.primary_key
        point_id: PointId
        if len(pk_cols) == 1:
            point_id = _coerce_scalar(raw.get(pk_cols[0]))
        else:
            point_id = {col: _coerce_scalar(raw.get(col)) for col in pk_cols}

        projection: dict[str, Any] = {}
        for logical_name, column in self._config.mapping.configured_items():
            value = raw.get(column)
            if value is None:
                warnings.append(f"missing_mapped_field:{logical_name}")
                continue
            if logical_name == "acl":
                value = _coerce_acl_value(value, warnings)
            elif logical_name == "tenant":
                value = str(value)
            else:
                value = _coerce_scalar(value)
            projection[logical_name] = value

        vector_names: list[str] = []
        vector_dimensions: dict[str, int] = {}
        if self._schema_cache is not None:
            for field_schema in self._schema_cache.vector_fields:
                vector_names.append(field_schema.name)
                vector_dimensions[field_schema.name] = field_schema.dimension

        vector_hashes: dict[str, str] | None = None
        if include_vectors:
            # FR-102: enabling vector retrieval is a resource-cost opt-in
            # (the vector column is now selected for every row instead
            # of being omitted), so it is always flagged here regardless
            # of whether this particular row's vector happened to be
            # present and parseable.
            warnings.append("vector_retrieval_enabled")
            raw_vector = raw.get(self._config.vector_column)
            if raw_vector is None:
                warnings.append("vector_missing")
            else:
                parsed = _parse_pgvector_literal(raw_vector)
                if parsed is None:
                    warnings.append("vector_shape_unrecognized")
                else:
                    vector_hashes = {self._config.vector_column: hash_vector(parsed)}

        scope = f"{self._config.schema_name}.{self._config.table}"
        return NormalizedPoint(
            target_id=scope,
            scope=scope,
            point_id=point_id,
            vector_names=vector_names,
            vector_dimensions=vector_dimensions,
            vector_hashes=vector_hashes,
            payload_projection=projection,
            payload_hash=compute_payload_hash(projection),
            source_id=projection.get("source_id"),
            source_version_id=projection.get("source_version_id"),
            chunk_id=projection.get("chunk_id"),
            embedding_id=projection.get("embedding_id"),
            acl=projection.get("acl"),
            tenant=projection.get("tenant"),
            observed_at=self._clock(),
            raw_locator=f"pgvector:{scope}#{_point_id_text(point_id)}",
            normalization_warnings=warnings,
        )

    def iterate_points(
        self,
        *,
        checkpoint: Checkpoint | None = None,
        projection: Sequence[str] | None = None,
        include_vectors: bool = False,
    ) -> Iterator[NormalizedPoint]:
        connection = self._ensure_connection()
        # A previous, only partially consumed pass may have left a
        # transaction open; starting from a clean slate guarantees this
        # pass gets its own fresh snapshot regardless of what happened
        # before it.
        connection.rollback()
        if self._schema_cache is None:
            self.inspect_target_schema()

        strict = self._config.consistency == "repeatable_read"
        start_count = self._count_rows(connection)

        query, params = self._build_query(checkpoint=checkpoint, include_vectors=include_vectors)
        cursor_name = f"ragledger_snapshot_{uuid.uuid4().hex[:8]}"
        raw_cursor = connection.cursor(name=cursor_name, row_factory=dict_row)
        cursor = _GuardedCursor(raw_cursor, connection)
        yielded = 0
        try:
            cursor.execute(query, params)
            while True:
                batch = cursor.fetchmany(self._config.fetch_size)
                if not batch:
                    break
                for raw_row in batch:
                    yielded += 1
                    point = self.normalize_point(dict(raw_row), include_vectors=include_vectors)
                    yield apply_projection(point, projection)
        finally:
            cursor.close()

        if strict:
            # Same REPEATABLE READ transaction backed both the count
            # probe and the row stream: no drift is possible.
            end_count = start_count
            mode = ConsistencyMode.STRICT_CONSISTENT
            completeness = SnapshotCompleteness.COMPLETE
            detail: str | None = None
            connection.commit()
        else:
            connection.commit()
            end_count = self._count_rows(connection)
            mode = ConsistencyMode.BEST_EFFORT_PAGED
            if start_count is None or end_count is None:
                completeness = SnapshotCompleteness.COMPLETE
                detail = "row count unavailable for one or both consistency probes"
            elif start_count != end_count:
                completeness = SnapshotCompleteness.INCOMPLETE
                detail = f"row count drifted from {start_count} to {end_count}"
            else:
                completeness = SnapshotCompleteness.COMPLETE
                detail = None

        self._consistency = ConsistencyInfo(
            mode=mode,
            completeness=completeness,
            start_count=start_count,
            end_count=end_count,
            observed_count=yielded,
            detail=detail,
        )

    def get_consistency_info(self) -> ConsistencyInfo:
        if self._consistency is None:
            raise RuntimeError("iterate_points has not completed a pass yet")
        return self._consistency

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    # -- internals -----------------------------------------------------

    def _configure_connection(self, connection: Any) -> None:
        connection.read_only = True
        if self._config.consistency == "repeatable_read":
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ

    def _ensure_connection(self) -> Any:
        if self._connection is not None:
            return self._connection
        if self._connection_factory is not None:
            try:
                connection = self._connection_factory()
            except Exception as exc:
                # Uniform with the real-`psycopg.connect` path below: any
                # connection-establishment failure -- auth, network, or a
                # test double simulating either -- surfaces as
                # `ConnectorConnectionError`, which `test_connection`
                # already knows how to report gracefully.
                raise ConnectorConnectionError(
                    f"failed to obtain pgvector connection: {exc}"
                ) from exc
            self._configure_connection(connection)
            self._connection = connection
            return connection

        dsn = self._config.resolve_dsn(env=self._env)
        attempt = 0
        delay = 0.5
        while True:
            try:
                connection = psycopg.connect(
                    dsn,
                    autocommit=False,
                    connect_timeout=max(1, int(self._config.connect_timeout_seconds)),
                    options=f"-c statement_timeout={self._config.statement_timeout_ms}",
                )
            except psycopg.OperationalError as exc:
                attempt += 1
                if attempt > self._config.max_connect_retries:
                    raise ConnectorConnectionError(
                        f"failed to connect to pgvector target after {attempt} attempt(s): {exc}"
                    ) from exc
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            break
        self._configure_connection(connection)
        self._connection = connection
        return connection

    def _table_ident(self) -> sql.Composed:
        return sql.SQL(".").join(
            [sql.Identifier(self._config.schema_name), sql.Identifier(self._config.table)]
        )

    def _where_fragments(self) -> tuple[list[sql.Composed], list[Any]]:
        fragments: list[sql.Composed] = []
        params: list[Any] = []
        for column, value in self._config.where.items():
            if isinstance(value, list):
                fragments.append(sql.SQL("{} = ANY(%s)").format(sql.Identifier(column)))
                params.append(list(value))
            else:
                fragments.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
                params.append(value)
        return fragments, params

    def _select_columns(self, *, include_vectors: bool) -> list[str]:
        columns = list(self._config.primary_key)
        for _, column in self._config.mapping.configured_items():
            if column not in columns:
                columns.append(column)
        if include_vectors and self._config.vector_column not in columns:
            columns.append(self._config.vector_column)
        return columns

    def _build_query(
        self, *, checkpoint: Checkpoint, include_vectors: bool
    ) -> tuple[sql.Composed, list[Any]]:
        columns = self._select_columns(include_vectors=include_vectors)
        select_list = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
        where_fragments, params = self._where_fragments()

        if checkpoint is not None:
            if not isinstance(checkpoint, dict):
                raise ConnectorConfigError(
                    "pgvector checkpoint must be a JSON object of primary key column values"
                )
            pk_cols = self._config.primary_key
            missing = [column for column in pk_cols if column not in checkpoint]
            if missing:
                raise ConnectorConfigError(f"checkpoint missing primary key column(s): {missing}")
            pk_tuple = sql.SQL(", ").join(sql.Identifier(column) for column in pk_cols)
            placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in pk_cols)
            where_fragments = [
                *where_fragments,
                sql.SQL("({}) > ({})").format(pk_tuple, placeholders),
            ]
            params = [*params, *(checkpoint[column] for column in pk_cols)]

        query = sql.SQL("SELECT {} FROM {}").format(select_list, self._table_ident())
        if where_fragments:
            query = query + sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where_fragments)
        order_list = sql.SQL(", ").join(
            sql.Identifier(column) for column in self._config.primary_key
        )
        query = query + sql.SQL(" ORDER BY ") + order_list
        return query, params

    def _count_rows(self, connection: Any) -> int | None:
        where_fragments, params = self._where_fragments()
        query = sql.SQL("SELECT count(*) AS row_count FROM {}").format(self._table_ident())
        if where_fragments:
            query = query + sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where_fragments)
        cursor = _GuardedCursor(connection.cursor(row_factory=dict_row), connection)
        try:
            cursor.execute(query, params)
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return None
        return int(row["row_count"])

    def _fetch_vector_dimension(self, connection: Any) -> int | None:
        cursor = _GuardedCursor(connection.cursor(row_factory=dict_row), connection)
        try:
            cursor.execute(
                sql.SQL(
                    "SELECT a.atttypmod AS atttypmod FROM pg_attribute a "
                    "JOIN pg_class c ON a.attrelid = c.oid "
                    "JOIN pg_namespace n ON c.relnamespace = n.oid "
                    "WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s "
                    "AND a.attnum > 0 AND NOT a.attisdropped"
                ),
                [self._config.schema_name, self._config.table, self._config.vector_column],
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return None
        atttypmod = row.get("atttypmod")
        if atttypmod is None or atttypmod <= 0:
            return None
        return int(atttypmod) - 4

    def _fetch_index_names(self, connection: Any) -> tuple[str, ...]:
        cursor = _GuardedCursor(connection.cursor(row_factory=dict_row), connection)
        try:
            cursor.execute(
                sql.SQL(
                    "SELECT i.relname AS index_name FROM pg_class t "
                    "JOIN pg_namespace n ON t.relnamespace = n.oid "
                    "JOIN pg_index ix ON ix.indrelid = t.oid "
                    "JOIN pg_class i ON i.oid = ix.indexrelid "
                    "WHERE n.nspname = %s AND t.relname = %s "
                    "ORDER BY i.relname"
                ),
                [self._config.schema_name, self._config.table],
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
        return tuple(row["index_name"] for row in rows)
