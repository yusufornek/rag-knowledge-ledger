"""Optional live integration tests against docker-compose.yml's Qdrant/Postgres.

Skipped by default; set `RAGLEDGER_IT=1` to run them against the
disposable local instances `docker-compose.yml` describes (Qdrant on
`localhost:26333`, Postgres on `localhost:25432`). Every fixture here
creates its own uniquely named, `ragledger_it_`-prefixed collection or
table and drops it again in a `finally` block, so a run never touches
anything else that might exist on those instances.

Setup/teardown intentionally goes through a *separate*, unguarded
admin client (a plain `httpx.Client` for Qdrant, a plain `psycopg`
connection for Postgres) -- never through `QdrantConnector` or
`PgvectorConnector`, which cannot create or delete anything by design.
The connectors under test are only ever used to *read* the fixture
data back, which is the whole point of this module: proving the
read-only connectors work end-to-end against real servers, and that
their section 42.2 mutation guards hold even outside the `httpx.MockTransport`/
fake-connection world the rest of this test package uses.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import httpx
import pytest

from ragledger.connectors.base import ConnectorMutationBlockedError
from ragledger.connectors.config import PgvectorTargetConfig, QdrantTargetConfig
from ragledger.connectors.qdrant import QdrantConnector

RAGLEDGER_IT = os.environ.get("RAGLEDGER_IT") == "1"
QDRANT_ENDPOINT = os.environ.get("RAGLEDGER_IT_QDRANT_ENDPOINT", "http://localhost:26333")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "ragledger-dev-only-insecure")
POSTGRES_DSN = os.environ.get(
    "RAGLEDGER_IT_PG_DSN",
    f"postgresql://ragledger:{POSTGRES_PASSWORD}@localhost:25432/ragledger",
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RAGLEDGER_IT,
        reason="set RAGLEDGER_IT=1 to run live connector integration tests",
    ),
]


def _unique_name(prefix: str) -> str:
    return f"ragledger_it_{prefix}_{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Qdrant
# --------------------------------------------------------------------------


@pytest.fixture
def qdrant_collection() -> Iterator[str]:
    name = _unique_name("qdrant")
    with httpx.Client(base_url=QDRANT_ENDPOINT, timeout=10.0) as admin:
        admin.put(
            f"/collections/{name}",
            json={"vectors": {"dense": {"size": 3, "distance": "Cosine"}}},
        ).raise_for_status()
        points = {
            "points": [
                {
                    "id": i,
                    "vector": {"dense": [0.1 * i, 0.2 * i, 0.3 * i]},
                    "payload": {
                        "ragledger": {"source_id": f"src_{i}", "chunk_id": f"chk_{i}"},
                        "tenant_id": "acme",
                        "allowed_groups": ["group:support"],
                    },
                }
                for i in range(1, 4)
            ]
        }
        try:
            admin.put(
                f"/collections/{name}/points", params={"wait": "true"}, json=points
            ).raise_for_status()
            yield name
        finally:
            admin.delete(f"/collections/{name}")


def _qdrant_config(collection: str) -> QdrantTargetConfig:
    return QdrantTargetConfig.model_validate(
        {
            "endpoint": QDRANT_ENDPOINT,
            "collection": collection,
            "vector_name": "dense",
            "payload_mapping": {
                "source_id": "ragledger.source_id",
                "chunk_id": "ragledger.chunk_id",
                "tenant": "tenant_id",
                "acl": "allowed_groups",
            },
        }
    )


def test_qdrant_connector_reads_back_live_collection(qdrant_collection: str) -> None:
    connector = QdrantConnector(_qdrant_config(qdrant_collection))
    try:
        assert connector.test_connection().ok is True
        schema = connector.inspect_target_schema()
        assert schema.vector_fields[0].dimension == 3
        points = list(connector.iterate_points())
        assert {point.point_id for point in points} == {1, 2, 3}
        assert {point.source_id for point in points} == {"src_1", "src_2", "src_3"}
        assert connector.get_consistency_info().completeness.value == "complete"
    finally:
        connector.close()


def test_qdrant_connector_read_only_guard_blocks_live_mutation(qdrant_collection: str) -> None:
    connector = QdrantConnector(_qdrant_config(qdrant_collection))
    try:
        with pytest.raises(ConnectorMutationBlockedError):
            connector._client.delete(f"/collections/{qdrant_collection}")
        # Prove the guard actually stopped it: the collection is still there.
        assert connector.test_connection().ok is True
    finally:
        connector.close()


# --------------------------------------------------------------------------
# pgvector
# --------------------------------------------------------------------------


@pytest.fixture
def pgvector_table() -> Iterator[str]:
    import psycopg
    from psycopg import sql

    table = _unique_name("pgvector")
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        admin.execute(
            sql.SQL(
                "CREATE TABLE {} (id serial primary key, source_id text, chunk_id text, "
                "tenant_id text, acl_json jsonb, embedding vector(3))"
            ).format(sql.Identifier(table))
        )
        for i in range(1, 4):
            admin.execute(
                sql.SQL(
                    "INSERT INTO {} (source_id, chunk_id, tenant_id, acl_json, embedding) "
                    "VALUES (%s, %s, %s, %s, %s)"
                ).format(sql.Identifier(table)),
                (
                    f"src_{i}",
                    f"chk_{i}",
                    "acme",
                    psycopg.types.json.Json(["group:support"]),
                    f"[{0.1 * i},{0.2 * i},{0.3 * i}]",
                ),
            )
        try:
            yield table
        finally:
            admin.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))


def _pgvector_config(table: str) -> PgvectorTargetConfig:
    return PgvectorTargetConfig.model_validate(
        {
            "dsn_env": "RAGLEDGER_IT_PG_DSN_RESOLVED",
            "schema": "public",
            "table": table,
            "primary_key": ["id"],
            "vector_column": "embedding",
            "mapping": {
                "source_id": "source_id",
                "chunk_id": "chunk_id",
                "tenant": "tenant_id",
                "acl": "acl_json",
            },
        }
    )


def test_pgvector_connector_reads_back_live_table(pgvector_table: str) -> None:
    from ragledger.connectors.pgvector import PgvectorConnector

    connector = PgvectorConnector(
        _pgvector_config(pgvector_table),
        env={"RAGLEDGER_IT_PG_DSN_RESOLVED": POSTGRES_DSN},
    )
    try:
        assert connector.test_connection().ok is True
        schema = connector.inspect_target_schema()
        assert schema.vector_fields[0].dimension == 3
        points = list(connector.iterate_points())
        assert [point.source_id for point in points] == ["src_1", "src_2", "src_3"]
        assert connector.get_consistency_info().completeness.value == "complete"
    finally:
        connector.close()


def test_pgvector_connection_is_read_only_at_database_level(pgvector_table: str) -> None:
    import psycopg
    from psycopg import sql

    from ragledger.connectors.pgvector import PgvectorConnector

    connector = PgvectorConnector(
        _pgvector_config(pgvector_table),
        env={"RAGLEDGER_IT_PG_DSN_RESOLVED": POSTGRES_DSN},
    )
    try:
        # Deliberately bypasses this module's own application-level
        # statement-whitelist guard to prove the second, independent
        # layer: the connection itself was opened in a read-only
        # transaction, so PostgreSQL rejects the write regardless of
        # what issued it.
        connection = connector._ensure_connection()
        raw_cursor = connection.cursor()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            raw_cursor.execute(
                sql.SQL("INSERT INTO {} (source_id) VALUES (%s)").format(
                    sql.Identifier(pgvector_table)
                ),
                ("hacker",),
            )
        connection.rollback()
    finally:
        connector.close()
