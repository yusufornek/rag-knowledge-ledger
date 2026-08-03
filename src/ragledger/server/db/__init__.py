"""SQLAlchemy 2.0 persistence layer for the server, per PROJECT_SPEC.md section 15/36.

`ragledger.server.db.base` defines the shared `Base` declarative class,
naming conventions, and the UUIDv7 identity helper section 15.3
requires ("Internal UUIDv7; portable identity unique text/binary hash
indexed"). `ragledger.server.db.models` holds the ORM models for the
entities this wave covers. `ragledger.server.db.session` builds an
engine/session factory from `ragledger.server.settings.Settings`.

Migrations live under `ragledger/server/migrations/` (Alembic), driven
by `alembic.ini` at the repository root.
"""

from __future__ import annotations

from ragledger.server.db import models as models  # noqa: F401  (registers all ORM models on Base)
from ragledger.server.db.base import Base
from ragledger.server.db.session import make_engine, make_session_factory

__all__ = ["Base", "make_engine", "make_session_factory", "models"]
