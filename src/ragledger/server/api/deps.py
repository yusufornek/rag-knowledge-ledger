"""Authentication and authorization dependencies for `/api/v1`, per FR-002.

The chain every scoped route declares is:

1. `authenticate` -- parse the ``Authorization: Bearer rlk_...`` header,
   look the token up by its public selector (one indexed query), verify
   the presented secret against the stored salt/hash in constant time
   (`ragledger.server.security.verify_api_token`), and reject revoked or
   expired rows. Success stamps `last_used_at`.
2. `require_workspace` -- the authenticated token's `workspace_id` must
   equal the ``{workspace_id}`` path parameter. A mismatch is a 404,
   not a 403: FR-002/section 19.2's cross-workspace IDOR rule means a
   caller must not be able to distinguish "exists in another workspace"
   from "does not exist".
3. `require_scope(...)` -- the token's `scopes` array must contain the
   route's scope, or ``admin``, which implies every other scope
   (section 8.1 lists admin as a scope, and a token that can mint other
   tokens can trivially grant itself any scope anyway -- modeling that
   as implication keeps the check honest).

Failures are RFC 9457 problems via
`ragledger.server.api.problems.ProblemException`; 401s carry a
``WWW-Authenticate: Bearer`` header per RFC 6750 (added by the app's
problem handler contract below).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Path, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ragledger.server.api.problems import ProblemException, problem_type
from ragledger.server.app import get_db_session
from ragledger.server.db.models import ApiToken
from ragledger.server.security import token_selector, verify_api_token

__all__ = [
    "API_TOKEN_SCOPES",
    "AuthContext",
    "authenticate",
    "require_scope",
    "require_workspace",
]

# FR-002's closed scope vocabulary, verbatim.
API_TOKEN_SCOPES = frozenset(
    {
        "sources",
        "builds",
        "targets",
        "snapshots",
        "reconciliations",
        "policies",
        "admin",
    }
)


@dataclass(frozen=True)
class AuthContext:
    """The caller a route handler acts on behalf of."""

    token_id: uuid.UUID
    workspace_id: uuid.UUID
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes


def _unauthorized(detail: str) -> ProblemException:
    return ProblemException(
        status=401,
        title="Unauthorized",
        detail=detail,
        problem_type=problem_type("unauthorized"),
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate(
    request: Request,
    db: Annotated[Session, Depends(get_db_session)],
) -> AuthContext:
    """Resolve the request's bearer token into an `AuthContext`, or raise a 401 problem."""
    header = request.headers.get("Authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        raise _unauthorized("missing bearer token")
    token = credentials.strip()

    selector = token_selector(token)
    if selector is None:
        raise _unauthorized("malformed bearer token")

    row = db.execute(select(ApiToken).where(ApiToken.selector == selector)).scalar_one_or_none()
    if row is None:
        raise _unauthorized("unknown token")
    if not verify_api_token(token, salt=row.salt, expected_hash=row.token_hash):
        raise _unauthorized("invalid token")
    if row.revoked_at is not None:
        raise _unauthorized("token has been revoked")
    if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
        raise _unauthorized("token has expired")

    row.last_used_at = datetime.now(UTC)
    db.commit()

    return AuthContext(
        token_id=row.id,
        workspace_id=row.workspace_id,
        scopes=frozenset(row.scopes),
    )


def require_workspace(
    workspace_id: Annotated[uuid.UUID, Path()],
    auth: Annotated[AuthContext, Depends(authenticate)],
) -> AuthContext:
    """Bind the authenticated token to the ``{workspace_id}`` path segment.

    A mismatch renders exactly the same 404 problem an unknown id
    would, so a probing caller learns nothing about other workspaces.
    """
    if auth.workspace_id != workspace_id:
        raise ProblemException(
            status=404,
            title="Not found",
            detail="workspace not found",
            problem_type=problem_type("not-found"),
        )
    return auth


def require_scope(scope: str) -> object:
    """A dependency asserting the token carries ``scope`` (or ``admin``)."""
    if scope not in API_TOKEN_SCOPES:  # programming error, not caller error
        raise ValueError(f"unknown scope {scope!r}")

    def _check(auth: Annotated[AuthContext, Depends(require_workspace)]) -> AuthContext:
        if not auth.has_scope(scope):
            raise ProblemException(
                status=403,
                title="Forbidden",
                detail=f"this operation requires the {scope!r} scope",
                problem_type=problem_type("insufficient-scope"),
            )
        return auth

    return Depends(_check)
