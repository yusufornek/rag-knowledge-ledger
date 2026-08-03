"""Server configuration, per PROJECT_SPEC.md section 41.

`Settings` is a `pydantic-settings` model that reads the environment
variable names section 41 defines verbatim (`APP_ENV`, `DATABASE_URL`,
`REDIS_URL`, `APP_ENCRYPTION_KEY_V1`, `SESSION_SECRET`, the `PIPELINE_*`
/ `TARGET_*` / retention / observability variables, and so on) plus the
dedicated signing-key secret-mount variables section 41 calls out
separately (`MANIFEST_SIGNING_KEY_FILE`, `MANIFEST_SIGNING_KEY_ID`,
`MANIFEST_TRUST_STORE_PATH`).

Two things section 41 asks for that have no natural home in that
env-var list are also modeled here, because this wave's deliverables
need them: the FastAPI process's own bind address (`APP_HOST`/
`APP_PORT`) and the local content-addressed artifact store root
(`ARTIFACT_STORE_ROOT`, consumed by `ragledger.core.artifacts.ArtifactStore`)
used when no `OBJECT_STORE_*` backend is configured. Neither name
appears in PROJECT_SPEC.md's section 41 listing; see
`IMPLEMENTATION_STATUS.md` for this interpretation decision.

Validation posture, per section 41's prose: unknown *environment*
variables are never an error here (a process environment routinely
carries variables this application does not care about) -- `extra`
is `"ignore"`. What section 41 says *is* a hard error is an unknown
key in an app *config file*; this wave introduces no config-file
loader (nothing else in the project has one yet), so that half of the
rule has nothing to attach to yet and is not implemented -- see
`IMPLEMENTATION_STATUS.md`.

Secrets (`DATABASE_URL`, `REDIS_URL`, the `APP_ENCRYPTION_KEY_V*`
keyring, `SESSION_SECRET`, `OBJECT_STORE_ACCESS_KEY`/`_SECRET_KEY`) are
modeled as `pydantic.SecretStr` so they never render in plain text via
`repr()`, `str()`, or `.model_dump()`; `Settings.masked_dict` gives
callers (structured logging, `/version`-style debug endpoints) an
explicitly safe-to-log view.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from ipaddress import ip_network
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

_APP_ENV_VALUES = ("development", "test", "production")
_RETENTION_MODES = ("retain", "purge_after_build")
_ENCRYPTION_KEY_VAR_RE = re.compile(r"^APP_ENCRYPTION_KEY_V(\d+)$")


class Settings(BaseSettings):
    """Strict, fail-fast server configuration read from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # -- Runtime environment -------------------------------------------------
    app_env: Literal["development", "test", "production"] = Field(
        default="development", alias="APP_ENV"
    )
    app_base_url: str = Field(default="http://localhost:8000", alias="APP_BASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # -- Process bind address (not in section 41's list; see module docstring) --
    app_host: str = Field(default="127.0.0.1", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")

    # -- Persistence ----------------------------------------------------------
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+psycopg://ragledger:ragledger@localhost:25433/ragledger"),
        alias="DATABASE_URL",
    )
    redis_url: SecretStr = Field(default=SecretStr("redis://localhost:26379/0"), alias="REDIS_URL")

    # -- Object storage (wave B consumer; modeled now per section 41) --------
    object_store_endpoint: str | None = Field(default=None, alias="OBJECT_STORE_ENDPOINT")
    object_store_bucket: str = Field(default="ragledger", alias="OBJECT_STORE_BUCKET")
    object_store_access_key: SecretStr | None = Field(default=None, alias="OBJECT_STORE_ACCESS_KEY")
    object_store_secret_key: SecretStr | None = Field(default=None, alias="OBJECT_STORE_SECRET_KEY")

    # -- Local artifact store root (not in section 41's list; see docstring) --
    artifact_store_root: Path = Field(default=Path("./data/artifacts"), alias="ARTIFACT_STORE_ROOT")

    # -- Secrets ---------------------------------------------------------------
    session_secret: SecretStr | None = Field(default=None, alias="SESSION_SECRET")

    # -- API tokens (not in section 41's list; see docstring) ------------------
    api_token_prefix: str = Field(default="rlk", alias="API_TOKEN_PREFIX")

    # -- Pipeline defaults ------------------------------------------------------
    pipeline_runner_image: str | None = Field(default=None, alias="PIPELINE_RUNNER_IMAGE")
    pipeline_cpu_default: int = Field(default=2, gt=0, alias="PIPELINE_CPU_DEFAULT")
    pipeline_memory_mb_default: int = Field(default=4096, gt=0, alias="PIPELINE_MEMORY_MB_DEFAULT")
    pipeline_file_bytes_max: int = Field(default=104_857_600, gt=0, alias="PIPELINE_FILE_BYTES_MAX")
    pipeline_pdf_pages_max: int = Field(default=500, gt=0, alias="PIPELINE_PDF_PAGES_MAX")
    embedding_batch_size_max: int = Field(default=256, gt=0, alias="EMBEDDING_BATCH_SIZE_MAX")

    # -- Target access ------------------------------------------------------------
    target_connect_timeout_seconds: int = Field(
        default=10, gt=0, alias="TARGET_CONNECT_TIMEOUT_SECONDS"
    )
    target_read_timeout_seconds: int = Field(default=60, gt=0, alias="TARGET_READ_TIMEOUT_SECONDS")
    target_page_size_max: int = Field(default=1000, gt=0, alias="TARGET_PAGE_SIZE_MAX")
    allow_private_targets: bool = Field(default=False, alias="ALLOW_PRIVATE_TARGETS")
    private_target_cidrs: str = Field(default="", alias="PRIVATE_TARGET_CIDRS")

    # -- Manifest / reconciliation limits -----------------------------------------
    manifest_inline_records_max: int = Field(
        default=50_000, gt=0, alias="MANIFEST_INLINE_RECORDS_MAX"
    )
    reconciliation_temp_bytes_max: int = Field(
        default=53_687_091_200, gt=0, alias="RECONCILIATION_TEMP_BYTES_MAX"
    )

    # -- Retention ------------------------------------------------------------------
    # PROJECT_SPEC.md section 41: `RAW_SOURCE_RETENTION_DAYS=0` is ambiguous
    # between "workspace policy default" and "immediate purge", so the final
    # implementation must use an explicit mode plus optional days instead.
    raw_source_retention_mode: Literal["retain", "purge_after_build"] = Field(
        default="retain", alias="RAW_SOURCE_RETENTION_MODE"
    )
    raw_source_retention_days: int | None = Field(
        default=None, ge=0, alias="RAW_SOURCE_RETENTION_DAYS"
    )
    parsed_artifact_retention_days: int = Field(
        default=30, ge=0, alias="PARSED_ARTIFACT_RETENTION_DAYS"
    )
    snapshot_artifact_retention_days: int = Field(
        default=30, ge=0, alias="SNAPSHOT_ARTIFACT_RETENTION_DAYS"
    )
    report_retention_days: int = Field(default=180, ge=0, alias="REPORT_RETENTION_DAYS")

    # -- Observability ---------------------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )

    # -- Manifest signing (dedicated secret-mount variables, section 41) -------------
    manifest_signing_key_file: Path | None = Field(default=None, alias="MANIFEST_SIGNING_KEY_FILE")
    manifest_signing_key_id: str | None = Field(default=None, alias="MANIFEST_SIGNING_KEY_ID")
    manifest_trust_store_path: Path | None = Field(default=None, alias="MANIFEST_TRUST_STORE_PATH")

    # -- Credential encryption keyring (scanned from the environment; see below) -----
    encryption_keys: dict[str, SecretStr] = Field(default_factory=dict, exclude=True)
    encryption_key_current_id: str | None = Field(default=None, exclude=True)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError(f"LOG_LEVEL {value!r} is not a recognized logging level name")
        return level

    @field_validator("private_target_cidrs")
    @classmethod
    def _validate_private_target_cidrs(cls, value: str) -> str:
        for entry in _split_csv(value):
            try:
                ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"PRIVATE_TARGET_CIDRS entry {entry!r} is not a valid CIDR"
                ) from exc
        return value

    @model_validator(mode="after")
    def _load_encryption_keyring(self) -> Settings:
        keys: dict[str, SecretStr] = {}
        highest_version = -1
        current_id: str | None = None
        for var_name, raw_value in os.environ.items():
            match = _ENCRYPTION_KEY_VAR_RE.match(var_name)
            if match is None or not raw_value:
                continue
            key_id = f"v{match.group(1)}"
            keys[key_id] = SecretStr(raw_value)
            version = int(match.group(1))
            if version > highest_version:
                highest_version = version
                current_id = key_id
        self.encryption_keys = keys
        self.encryption_key_current_id = current_id
        return self

    @model_validator(mode="after")
    def _require_production_secrets(self) -> Settings:
        if self.app_env != "production":
            return self
        missing = []
        if self.session_secret is None:
            missing.append("SESSION_SECRET")
        if not self.encryption_keys:
            missing.append("APP_ENCRYPTION_KEY_V1")
        if missing:
            raise ValueError("missing required production secrets: " + ", ".join(missing))
        return self

    @model_validator(mode="after")
    def _require_retention_days_consistency(self) -> Settings:
        if (
            self.raw_source_retention_mode == "purge_after_build"
            and self.raw_source_retention_days is not None
        ):
            raise ValueError(
                "RAW_SOURCE_RETENTION_DAYS must not be set when "
                "RAW_SOURCE_RETENTION_MODE=purge_after_build (raw sources are "
                "purged immediately after the build, not retained for a count "
                "of days)"
            )
        return self

    @property
    def signing_enabled(self) -> bool:
        """Whether manifest signing is available in this process.

        Per PROJECT_SPEC.md section 41: "Web server signing kapalıysa key
        file yok ve feature status disabled; build tamamlanabilir
        unsigned" -- signing is opportunistic, not required, so this is a
        soft on/off flag rather than a validation failure when unset.
        """
        return (
            self.manifest_signing_key_file is not None and self.manifest_signing_key_file.is_file()
        )

    def require_encryption_key(self, key_id: str) -> SecretStr:
        """Return the encryption key registered under ``key_id`` (for example ``"v1"``).

        Raises `KeyError` with a message safe to surface (it never
        includes key material) if no such key is configured.
        """
        try:
            return self.encryption_keys[key_id]
        except KeyError as exc:
            raise KeyError(f"no encryption key configured for key id {key_id!r}") from exc

    def current_encryption_key(self) -> tuple[str, SecretStr]:
        """Return ``(key_id, key)`` for the highest-numbered configured `APP_ENCRYPTION_KEY_V*`.

        Raises `RuntimeError` if no `APP_ENCRYPTION_KEY_V*` variable is set.
        """
        if self.encryption_key_current_id is None:
            raise RuntimeError("no APP_ENCRYPTION_KEY_V<n> environment variable is configured")
        return self.encryption_key_current_id, self.encryption_keys[self.encryption_key_current_id]

    def masked_dict(self) -> dict[str, object]:
        """A safe-to-log view of this configuration: secrets replaced with a fixed mask."""
        masked: dict[str, object] = {}
        for name, field in type(self).model_fields.items():
            if name in {"encryption_keys", "encryption_key_current_id"}:
                continue
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                masked[field.alias or name] = "***" if value.get_secret_value() else None
            elif isinstance(value, Path):
                masked[field.alias or name] = str(value)
            else:
                masked[field.alias or name] = value
        masked["encryption_key_ids"] = sorted(self.encryption_keys)
        masked["encryption_key_current_id"] = self.encryption_key_current_id
        return masked

    @property
    def base_url(self) -> HttpUrl:
        return HttpUrl(self.app_base_url)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached `Settings` instance.

    Cached so repeated `Depends(get_settings)` calls in the FastAPI app
    (and repeated calls anywhere else) do not re-parse and re-validate
    the environment on every call; tests that need a fresh read should
    construct `Settings()` directly, or call `get_settings.cache_clear()`.
    """
    return Settings()
