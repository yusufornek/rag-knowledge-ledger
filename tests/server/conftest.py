"""Shared fixtures for `tests/server/`.

DB-backed tests connect to `RAGLEDGER_TEST_DATABASE_URL`, defaulting to
`docker-compose.yml`'s `appdb` service
(``postgresql+psycopg://ragledger:ragledger@localhost:25433/ragledger``).
That service is not started by default (see README.md), so these tests
make one short-timeout connection attempt at collection time and skip
cleanly (`requires_database`) when it fails, rather than error the
whole run -- but they genuinely exercise a real Postgres whenever one
is reachable, including in CI, where `.github/workflows/ci.yml` starts
one as a service container and points `RAGLEDGER_TEST_DATABASE_URL` at
it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from ragledger.server.db import models as models  # noqa: F401  (registers all tables)
from ragledger.server.db.base import Base

TEST_DATABASE_URL = os.environ.get(
    "RAGLEDGER_TEST_DATABASE_URL",
    "postgresql+psycopg://ragledger:ragledger@localhost:25433/ragledger",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "server_db: requires a reachable Postgres at RAGLEDGER_TEST_DATABASE_URL "
        "(docker-compose.yml's appdb service, or a CI service container); "
        "skipped automatically when unreachable.",
    )


def _database_reachable(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


requires_database = pytest.mark.skipif(
    not _database_reachable(TEST_DATABASE_URL),
    reason=(
        f"RAGLEDGER_TEST_DATABASE_URL ({TEST_DATABASE_URL!r}) is not reachable; "
        "start docker-compose.yml's appdb service, or point "
        "RAGLEDGER_TEST_DATABASE_URL elsewhere, to run these tests"
    ),
)


def _reset_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture
def db_engine() -> Iterator[Engine]:
    """A fresh schema on the test database, with every ORM table created directly from metadata.

    Used by tests that only care about model behavior (CRUD,
    constraints); `tests/server/test_migrations.py` exercises the
    actual Alembic migration path separately.
    """
    engine = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 2})
    _reset_schema(engine)
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        _reset_schema(engine)
        engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    with Session(db_engine) as session:
        yield session
