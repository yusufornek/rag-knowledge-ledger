"""The built-in web UI, served at ``/ui`` (design specification section 18).

A deliberately dependency-free single-page application: three static
files (HTML, CSS, JS) written by hand, no bundler, no node toolchain,
no third-party frontend package -- the same reproducibility posture as
the rest of the project applied to its UI. The page consumes the same
`/api/v1` surface any other client uses, authenticates with a bearer
API token the operator pastes in (held in ``sessionStorage`` for the
tab's lifetime only), and renders every value with ``textContent``,
never HTML injection.

Responses carry a strict Content-Security-Policy (`default-src 'none'`
plus explicit self-only script/style/connect) so even a hypothetical
markup injection cannot load foreign code, and
``X-Content-Type-Options: nosniff``. The design language follows
section 18.1: dense, neutral, one accent color, automatic light/dark
via ``prefers-color-scheme``, no decoration.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

__all__ = ["webui_router"]

_STATIC_DIR = Path(__file__).parent / "static"

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

webui_router = APIRouter(include_in_schema=False)


def _static(name: str, media_type: str) -> FileResponse:
    return FileResponse(
        _STATIC_DIR / name,
        media_type=media_type,
        headers={
            "Content-Security-Policy": _CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@webui_router.get("/")
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/ui/", status_code=307)


@webui_router.get("/ui")
@webui_router.get("/ui/")
def ui_index() -> FileResponse:
    return _static("index.html", "text/html; charset=utf-8")


@webui_router.get("/ui/app.js")
def ui_app_js() -> FileResponse:
    return _static("app.js", "text/javascript; charset=utf-8")


@webui_router.get("/ui/style.css")
def ui_style_css() -> FileResponse:
    return _static("style.css", "text/css; charset=utf-8")
