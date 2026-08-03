"""Tests for `ragledger.server.app.create_app`: `/healthz` and `/version` via an ASGI test client.

No database or Redis is required: both dependencies are unreachable in
this test environment by default, and `/healthz` is specifically
designed to report that as `"degraded"` rather than fail the request
(see `ragledger.server.app._check_database`/`_check_redis`).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ragledger import __version__
from ragledger.server.app import REQUEST_ID_HEADER, create_app
from ragledger.server.settings import Settings


def _client() -> TestClient:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://ragledger:ragledger@localhost:1/ragledger",  # type: ignore[call-arg]
        REDIS_URL="redis://localhost:1/0",  # type: ignore[call-arg]
    )
    return TestClient(create_app(settings))


class TestVersion:
    def test_returns_package_version_and_app_env(self) -> None:
        with _client() as client:
            response = client.get("/version")
        assert response.status_code == 200
        body = response.json()
        assert body["version"] == __version__
        assert body["app_env"] == "development"


class TestHealthz:
    def test_returns_200_and_degraded_when_dependencies_unreachable(self) -> None:
        with _client() as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["database"] == "unreachable"
        assert body["checks"]["redis"] == "unreachable"

    def test_never_raises_even_on_full_outage(self) -> None:
        # The key behavioral contract: a dependency outage is reported,
        # never allowed to turn into an unhandled exception / 500.
        with _client() as client:
            response = client.get("/healthz")
        assert response.status_code == 200


class TestRequestIdMiddleware:
    def test_generates_a_request_id_when_none_supplied(self) -> None:
        with _client() as client:
            response = client.get("/version")
        assert REQUEST_ID_HEADER in response.headers
        assert len(response.headers[REQUEST_ID_HEADER]) > 0

    def test_echoes_a_caller_supplied_request_id(self) -> None:
        with _client() as client:
            response = client.get("/version", headers={REQUEST_ID_HEADER: "caller-supplied-id"})
        assert response.headers[REQUEST_ID_HEADER] == "caller-supplied-id"

    def test_two_requests_without_a_supplied_id_get_different_ids(self) -> None:
        with _client() as client:
            first = client.get("/version")
            second = client.get("/version")
        assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]
