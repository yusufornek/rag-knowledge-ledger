"""FastAPI application factory: settings, DB session, `/healthz`, `/version`.

This wave deliberately exposes no domain endpoints -- build/snapshot/
reconciliation/job routes are wave B's job. What is here is the
process-level scaffolding every later route depends on: `create_app`
wires validated `Settings` (`ragledger.server.settings`) and a
SQLAlchemy session factory into `app.state`, installs a request-id
middleware and JSON structured logging (section 41: "Secretler
config/log dump'ta redakte" -- log lines never carry a `Settings`
secret field, only `Settings.masked_dict()`'s redacted view), and
exposes two endpoints:

- `GET /healthz`: best-effort liveness/readiness. Never raises on a
  down dependency (a database or Redis outage should not crash the
  health check itself) -- it reports `"degraded"` with the specific
  failing check instead, so an orchestrator can tell "the process is
  alive but not ready" apart from "the process is dead."
- `GET /version`: the running package version and `APP_ENV`, for a
  quick "what is actually deployed here" check.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import redis
from fastapi import Depends, FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ragledger import __version__
from ragledger.server.db.session import make_engine, make_session_factory
from ragledger.server.settings import Settings, get_settings

__all__ = ["create_app", "get_db_session"]

REQUEST_ID_HEADER = "X-Request-ID"
_HEALTH_CHECK_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger("ragledger.server")


class _JsonLogFormatter(logging.Formatter):
    """Renders each log record as one JSON line; never includes raw secret values.

    Callers control what ends up in a message/`extra`; this formatter's
    only responsibility is a stable, structured shape (timestamp,
    level, logger name, message, request id when present, exception
    info when present) -- it is not a secret scanner.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id
        if record.exc_info is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    """Install a single JSON-line handler on the root logger at ``settings.log_level``."""
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attaches a request id (incoming `X-Request-ID`, or a fresh UUID4) to every request.

    The id is stored on `request.state.request_id` for handlers/log
    records to pick up, and echoed back on the response so a caller
    can correlate their request with server-side logs/audit events.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={"request_id": request_id},
        )
        return response


def get_db_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: a request-scoped `Session` from `app.state.session_factory`."""
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _check_database(db: Session) -> str:
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 -- any DB failure means "not ok", never a 500
        logger.warning("healthz database check failed", exc_info=True)
        return "unreachable"
    return "ok"


def _check_redis(settings: Settings) -> str:
    try:
        client: redis.Redis = redis.Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
            socket_timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        try:
            client.ping()
        finally:
            client.close()
    except Exception:  # noqa: BLE001 -- any Redis failure means "not ok", never a 500
        logger.warning("healthz redis check failed", exc_info=True)
        return "unreachable"
    return "ok"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Pass ``settings`` explicitly in tests to avoid env coupling.
    """
    resolved_settings = settings if settings is not None else get_settings()
    configure_logging(resolved_settings)

    engine = make_engine(resolved_settings)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield
        engine.dispose()

    app = FastAPI(
        title="RAG Knowledge Ledger",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.add_middleware(RequestIdMiddleware)

    @app.get("/healthz")
    def healthz(db: Session = Depends(get_db_session)) -> dict[str, Any]:  # noqa: B008
        checks = {
            "database": _check_database(db),
            "redis": _check_redis(resolved_settings),
        }
        overall = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
        return {"status": overall, "checks": checks}

    @app.get("/version")
    def version() -> dict[str, Any]:
        return {"version": __version__, "app_env": resolved_settings.app_env}

    return app
