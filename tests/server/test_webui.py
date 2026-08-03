"""The built-in web UI at `/ui` (design specification section 18).

The UI is three static files served by the app itself; these tests
pin the contract that matters: the pages are served with the right
content types and strict security headers, the root path redirects to
the UI, the navigation covers section 18.2's screen list, the
JavaScript's API calls all target routes the server actually exposes,
and no static file smuggles in an external resource, an inline
script, or an emoji (section 18.1's design constraints).
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ragledger.server.app import create_app
from ragledger.server.settings import Settings
from tests.server.conftest import TEST_DATABASE_URL, requires_database

pytestmark = requires_database

_STATIC_DIR = (
    Path(__file__).parent.parent.parent / "src" / "ragledger" / "server" / "webui" / "static"
)

_NAV_SCREENS = [
    "Overview",
    "Sources",
    "Builds",
    "Manifests",
    "Targets",
    "Snapshots",
    "Reconciliations",
    "Policies",
    "Settings",
]


@pytest.fixture
def client(
    db_engine: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", base64.b64encode(b"k" * 32).decode("ascii"))
    monkeypatch.setenv("ARTIFACT_STORE_ROOT", str(tmp_path / "artifacts"))
    with TestClient(create_app(Settings())) as test_client:
        yield test_client


class TestServing:
    def test_root_redirects_to_the_ui(self, client: TestClient) -> None:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/ui/"

    def test_index_is_served_with_strict_security_headers(self, client: TestClient) -> None:
        response = client.get("/ui/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_assets_are_served_with_their_content_types(self, client: TestClient) -> None:
        js = client.get("/ui/app.js")
        css = client.get("/ui/style.css")
        assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
        assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")

    def test_ui_is_absent_from_the_openapi_schema(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert not any(path.startswith("/ui") for path in paths)


class TestContent:
    def test_navigation_covers_every_specified_screen(self, client: TestClient) -> None:
        html = client.get("/ui/").text
        for screen in _NAV_SCREENS:
            assert f">{screen}<" in html, f"navigation is missing the {screen} screen"

    def test_no_external_resources_or_inline_script(self) -> None:
        html = (_STATIC_DIR / "index.html").read_text()
        # No resource is loaded from another origin, matching the CSP.
        assert not re.search(r'(src|href)="https?://', html)
        assert 'src="/ui/app.js"' in html
        assert 'href="/ui/style.css"' in html
        # The only <script> is the external self-hosted file; no inline code.
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL)
        assert all(not body.strip() for body in scripts)

    def test_static_files_carry_no_emoji(self) -> None:
        for name in ("index.html", "app.js", "style.css"):
            text = (_STATIC_DIR / name).read_text()
            assert not any(0x1F300 <= ord(ch) <= 0x1FAFF for ch in text), name

    def test_javascript_never_uses_html_injection(self) -> None:
        js = (_STATIC_DIR / "app.js").read_text()
        assert "innerHTML" not in js
        assert "insertAdjacentHTML" not in js
        assert "document.write" not in js

    def test_every_api_path_the_ui_calls_exists_on_the_server(self, client: TestClient) -> None:
        """Static analysis: each `/api/v1` string literal in app.js must match a real route."""
        js = (_STATIC_DIR / "app.js").read_text()
        openapi_paths = list(client.get("/openapi.json").json()["paths"])

        called = set(re.findall(r'"/api/v1([^"]*)"', js))
        called |= {"/workspaces/{id}" + suffix for suffix in re.findall(r'ws\(\) \+ "([^"]+)"', js)}
        called = {path.split("?")[0] for path in called if path and not path.startswith(" ")}

        def matches(called_path: str) -> bool:
            # Normalize the UI's dynamic segments to a wildcard, then
            # compare against each OpenAPI template the same way. A call
            # site like `ws() + "/builds/" + id + ":cancel"` only yields
            # the `/builds/` literal here, so a captured path ending in
            # "/" is a concatenation prefix: it matches when some real
            # route continues where the literal stops.
            pattern = re.sub(r"\{[^}]+\}", "*", called_path)
            is_prefix = pattern.endswith("/")
            for known in openapi_paths:
                known_pattern = re.sub(r"\{[^}]+\}", "*", known.removeprefix("/api/v1"))
                if known_pattern == pattern:
                    return True
                if is_prefix and known_pattern.startswith(pattern):
                    return True
                candidate = re.escape(known_pattern).replace(r"\*", "[^/]+")
                if re.fullmatch(candidate, pattern):
                    return True
            return False

        static_calls = {
            path for path in called if path.startswith("/workspaces/{id}") or path == "/workspaces"
        }
        unmatched = [
            path for path in sorted(static_calls) if not matches(path.replace("{id}", "*"))
        ]
        assert unmatched == [], f"app.js calls routes the server does not expose: {unmatched}"
