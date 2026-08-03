"""Engine and session factory construction from `ragledger.server.settings.Settings`.

Kept deliberately small: this module only builds SQLAlchemy's own
`Engine`/`sessionmaker` objects from a validated `Settings.database_url`.
Request-scoped session lifecycle (FastAPI `Depends`) lives in
`ragledger.server.app`, which is the layer that knows about the request
lifecycle; this module is reusable from a script, a test, or a worker
process just as well as from the API process.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from ragledger.server.settings import Settings


def make_engine(settings: Settings, *, echo: bool = False) -> Engine:
    """Build a SQLAlchemy engine for ``settings.database_url``.

    ``pool_pre_ping`` is enabled so a connection that went stale while
    idle in the pool (for example a Postgres server restart) is
    detected and replaced rather than surfaced as a confusing mid-query
    error.
    """
    return create_engine(
        settings.database_url.get_secret_value(),
        echo=echo,
        pool_pre_ping=True,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a `sessionmaker` bound to ``engine`` with explicit, predictable flush behavior."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
