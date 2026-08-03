"""Append-only audit event writer, per PROJECT_SPEC.md section 15.1/15.3/19.

Section 15.3: "Audit append-only, monthly partition readiness." This
module exposes exactly one write path (`AuditLog.record`), which only
ever inserts a new `ragledger.server.db.models.AuditEvent` row -- there
is no update or delete method here, by design, so "append-only" is a
property of the API surface itself, not just a documented convention.

Section 19.2's "PII in logs/reports: Value-free findings, HMAC, masking,
allowlist logs" applies here too: `AuditLog.record`'s `metadata` argument
is validated against `_FORBIDDEN_METADATA_KEYS` (case-insensitively) so
an obviously secret-shaped field (password, token, credential, api key,
...) can never make it into `AuditEvent.metadata_json`, catching the
easy mistake of a caller accidentally passing a raw secret through.
This is a best-effort guard, not a content scanner: it cannot stop a
caller from putting a secret under an innocuously named key, so callers
must still only ever pass identifiers and outcome data here, never
request/response bodies.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from ragledger.server.db.models import AuditEvent

__all__ = ["AuditLog", "AuditMetadataError"]

_FORBIDDEN_METADATA_KEY_PATTERN = re.compile(
    r"(password|secret|token|credential|api[_-]?key|private[_-]?key|dsn|connection[_-]?string)",
    re.IGNORECASE,
)


class AuditMetadataError(ValueError):
    """Raised when `AuditLog.record`'s `metadata` contains an obviously secret-shaped key."""


def _check_metadata(metadata: dict[str, Any] | None) -> None:
    if metadata is None:
        return
    for key in metadata:
        if _FORBIDDEN_METADATA_KEY_PATTERN.search(key):
            raise AuditMetadataError(
                f"audit metadata key {key!r} looks like it may carry a secret; "
                "never pass raw secrets/PII to AuditLog.record"
            )


class AuditLog:
    """A thin, insert-only wrapper around the `audit_events` table.

    Takes a caller-managed `Session` rather than owning its own engine
    or transaction, so a caller can write an audit event in the same
    transaction as the operation it describes (an audit row for an
    operation that then rolls back should not persist either).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        actor_type: str,
        action: str,
        result: str,
        workspace_id: uuid.UUID | None = None,
        actor_id: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Insert and return a new `AuditEvent`. Never updates or deletes an existing row.

        Raises `AuditMetadataError` if ``metadata`` contains a key that
        looks like it carries a secret (see the module docstring).
        """
        _check_metadata(metadata)
        event = AuditEvent(
            workspace_id=workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            result=result,
            request_id=request_id,
            metadata_json=metadata,
        )
        self._session.add(event)
        self._session.flush()
        return event
