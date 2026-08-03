"""`alembic upgrade head` against a genuinely empty database, and back down again.

Deliverable 3's "`alembic upgrade head` must work against Postgres" --
this drives the real Alembic entrypoint (`alembic.command`, the same
machinery `alembic upgrade head` on the command line uses), not a
shortcut through `Base.metadata.create_all` directly (that path is
exercised separately by `tests/server/conftest.py`'s `db_engine`
fixture, which the model/CRUD tests use). `DATABASE_URL` is
monkeypatched for the duration of each test so `alembic/env.py`'s
`Settings().database_url` resolves to the test database without
mutating anything else in the process environment.

Skips cleanly when `RAGLEDGER_TEST_DATABASE_URL` is unreachable (see
`tests/server/conftest.py`); runs for real in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tests.server.conftest import TEST_DATABASE_URL, requires_database

pytestmark = requires_database

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"

_EXPECTED_TABLES = {
    "workspaces",
    "users",
    "memberships",
    "api_tokens",
    "source_collections",
    "source_assets",
    "source_versions",
    "pipeline_configs",
    "builds",
    "manifests",
    "manifest_signatures",
    "vector_targets",
    "inventory_snapshots",
    "reconciliations",
    "findings",
    "lineage_index",
    "policies",
    "policy_revisions",
    "policy_evaluations",
    "audit_events",
    "jobs",
}


@pytest.fixture
def clean_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resets the test database to a truly empty (no tables, no `alembic_version`) state."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 2})
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    yield
    engine = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 2})
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def test_upgrade_head_creates_every_table(clean_database: None) -> None:
    assert _ALEMBIC_INI.is_file()
    config = Config(str(_ALEMBIC_INI))

    command.upgrade(config, "head")

    engine = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 2})
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert _EXPECTED_TABLES.issubset(table_names)
    assert "alembic_version" in table_names


def test_downgrade_base_removes_every_table(clean_database: None) -> None:
    config = Config(str(_ALEMBIC_INI))
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    engine = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 2})
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert _EXPECTED_TABLES.isdisjoint(table_names)
