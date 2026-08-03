"""Tests for `ragledger.server.settings.Settings`."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from ragledger.server.settings import Settings, get_settings


def _clear_env_prefix(monkeypatch: pytest.MonkeyPatch, *prefixes: str) -> None:
    for name in list(os.environ):
        if name.startswith(prefixes):
            monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_development_defaults_construct_without_any_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env_prefix(monkeypatch, "APP_", "DATABASE_", "REDIS_", "SESSION_")
        settings = Settings()
        assert settings.app_env == "development"
        assert settings.log_level == "INFO"
        assert settings.raw_source_retention_mode == "retain"

    def test_get_settings_is_cached(self) -> None:
        get_settings.cache_clear()
        first = get_settings()
        second = get_settings()
        assert first is second
        get_settings.cache_clear()


class TestValidation:
    def test_unknown_app_env_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "staging")
        with pytest.raises(ValidationError):
            Settings()

    def test_bad_log_level_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")
        with pytest.raises(ValidationError):
            Settings()

    def test_log_level_is_case_insensitive_and_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "debug")
        assert Settings().log_level == "DEBUG"

    def test_invalid_private_target_cidr_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRIVATE_TARGET_CIDRS", "not-a-cidr")
        with pytest.raises(ValidationError):
            Settings()

    def test_valid_private_target_cidrs_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRIVATE_TARGET_CIDRS", "10.0.0.0/8, 192.168.1.0/24")
        settings = Settings()
        assert settings.private_target_cidrs == "10.0.0.0/8, 192.168.1.0/24"

    def test_raw_source_retention_days_conflicts_with_purge_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAW_SOURCE_RETENTION_MODE", "purge_after_build")
        monkeypatch.setenv("RAW_SOURCE_RETENTION_DAYS", "30")
        with pytest.raises(ValidationError):
            Settings()

    def test_production_without_secrets_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        _clear_env_prefix(monkeypatch, "APP_ENCRYPTION_KEY_V")
        with pytest.raises(ValidationError, match="SESSION_SECRET"):
            Settings()

    def test_production_with_secrets_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("SESSION_SECRET", "a-real-secret")
        monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", "a-real-key")
        settings = Settings()
        assert settings.app_env == "production"


class TestEncryptionKeyring:
    def test_no_keys_configured_yields_empty_keyring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env_prefix(monkeypatch, "APP_ENCRYPTION_KEY_V")
        settings = Settings()
        assert settings.encryption_keys == {}
        with pytest.raises(RuntimeError):
            settings.current_encryption_key()

    def test_highest_version_wins_as_current(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", "key-one")
        monkeypatch.setenv("APP_ENCRYPTION_KEY_V2", "key-two")
        settings = Settings()
        key_id, key = settings.current_encryption_key()
        assert key_id == "v2"
        assert key.get_secret_value() == "key-two"
        assert settings.require_encryption_key("v1").get_secret_value() == "key-one"

    def test_unknown_key_id_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", "key-one")
        settings = Settings()
        with pytest.raises(KeyError):
            settings.require_encryption_key("v99")


class TestMaskedDict:
    def test_secrets_are_masked_not_leaked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:hunter2@host/db")
        settings = Settings()
        masked = settings.masked_dict()
        dumped = repr(masked)
        assert "hunter2" not in dumped
        assert masked["DATABASE_URL"] == "***"

    def test_repr_never_contains_secret_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSION_SECRET", "extremely-secret-value")
        settings = Settings()
        assert "extremely-secret-value" not in repr(settings)
        assert "extremely-secret-value" not in str(settings)
