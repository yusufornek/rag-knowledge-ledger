"""The `/api/v1` HTTP surface (M7 wave B), per PROJECT_SPEC.md section 16.

`ragledger.server.api.routes.api_router` holds the route handlers;
`ragledger.server.api.deps` the authentication/scope dependency chain;
`ragledger.server.api.problems` the RFC 9457 error rendering;
`ragledger.server.api.schemas` the request/response DTOs. The FastAPI
application factory (`ragledger.server.app.create_app`) mounts the
router under ``/api/v1`` and installs the problem handlers.
"""

from __future__ import annotations

from ragledger.server.api.routes import api_router

__all__ = ["api_router"]
