"""RFC 9457 problem-details error responses, per the design specification section 16.

Section 16: "Base `/api/v1`, RFC 9457, cursor pagination, idempotency
keys." Every error body an `/api/v1` route produces is an
``application/problem+json`` document with `type`, `title`, `status`,
`detail`, and `instance` members -- including FastAPI's own request
validation errors, which are re-shaped here rather than left in their
default ``{"detail": [...]}`` form.

`ProblemException` is the one exception type route handlers raise for
expected failures. Handlers never put secret material or another
workspace's identifiers into `detail`; the cross-workspace convention
(see `ragledger.server.api.deps`) is that a resource outside the
caller's workspace produces the same 404 problem as one that does not
exist at all, so a response cannot be used as an existence oracle.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = ["ProblemException", "install_problem_handlers", "problem_response"]

_PROBLEM_TYPE_BASE = "https://ragledger.dev/problems"
PROBLEM_CONTENT_TYPE = "application/problem+json"


class ProblemException(Exception):
    """An expected, caller-visible API failure, rendered as an RFC 9457 problem."""

    def __init__(
        self,
        *,
        status: int,
        title: str,
        detail: str,
        problem_type: str = "about:blank",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.problem_type = problem_type
        self.headers = headers or {}


def problem_type(slug: str) -> str:
    """A stable, documentation-pointing `type` URI for a known problem category."""
    return f"{_PROBLEM_TYPE_BASE}/{slug}"


def problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    detail: str,
    problem_type_uri: str = "about:blank",
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": problem_type_uri,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": str(request.url.path),
    }
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_CONTENT_TYPE)


def install_problem_handlers(app: FastAPI) -> None:
    """Register handlers so every error leaving `/api/v1` is a problem document."""

    @app.exception_handler(ProblemException)
    async def _problem_exception_handler(request: Request, exc: ProblemException) -> JSONResponse:
        response = problem_response(
            request,
            status=exc.status,
            title=exc.title,
            detail=exc.detail,
            problem_type_uri=exc.problem_type,
        )
        for name, value in exc.headers.items():
            response.headers[name] = value
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return problem_response(
            request,
            status=exc.status_code,
            title="HTTP error",
            detail=str(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # `exc.errors()` entries carry the submitted value under "input";
        # a validation failure on a credential field must not echo the
        # credential back, so only location/message/type survive.
        errors = [
            {"loc": error.get("loc"), "msg": error.get("msg"), "type": error.get("type")}
            for error in exc.errors()
        ]
        return problem_response(
            request,
            status=422,
            title="Request validation failed",
            detail="one or more request fields failed validation",
            problem_type_uri=problem_type("request-validation"),
            extra={"errors": errors},
        )
