"""Tests for `ragledger.server.audit.AuditLog`.

The metadata-rejection tests below need no database: `AuditLog.record`
validates `metadata` and raises before it ever touches the session, so
a bare object (never dereferenced) stands in for a `Session`. A real
insert (`test_record_persists_a_new_audit_event`, in
`tests/server/test_db.py`) needs a live database and lives there
instead, guarded by `requires_database`.
"""

from __future__ import annotations

import pytest

from ragledger.server.audit import AuditLog, AuditMetadataError


class _UnusedSession:
    """Stands in for a `Session` in tests that must never actually call it."""

    def add(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("session.add must not be called when metadata validation fails")

    def flush(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("session.flush must not be called when metadata validation fails")


class TestMetadataValidation:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "Password",
            "api_key",
            "api-key",
            "apikey",
            "token",
            "access_token",
            "credential",
            "private_key",
            "dsn",
            "connection_string",
            "SECRET",
        ],
    )
    def test_secret_shaped_keys_are_rejected(self, key: str) -> None:
        audit = AuditLog(_UnusedSession())  # type: ignore[arg-type]
        with pytest.raises(AuditMetadataError):
            audit.record(
                actor_type="user",
                action="target.create",
                result="success",
                metadata={key: "irrelevant"},
            )

    @pytest.mark.parametrize("key", ["target_name", "status_code", "duration_ms", "count"])
    def test_ordinary_keys_pass_validation(self, key: str) -> None:
        # These do not touch the database (still using the raising stub), so
        # a validation *pass* is observed as "no AuditMetadataError raised
        # before the (expected) AssertionError from touching the session."
        audit = AuditLog(_UnusedSession())  # type: ignore[arg-type]
        with pytest.raises(AssertionError):
            audit.record(
                actor_type="user",
                action="target.create",
                result="success",
                metadata={key: "value"},
            )

    def test_none_metadata_is_allowed(self) -> None:
        audit = AuditLog(_UnusedSession())  # type: ignore[arg-type]
        with pytest.raises(AssertionError):
            audit.record(actor_type="user", action="target.create", result="success")
