"""Target configuration models and env-var credential resolution, per section 35.

Two target config shapes are modeled here, matching section 35.1 and
35.2's YAML examples field-for-field: `QdrantTargetConfig` and
`PgvectorTargetConfig`. Both:

- Never carry a resolved secret as a field. `QdrantTargetConfig.api_key_env`
  and `PgvectorTargetConfig.dsn_env` are environment variable *names*;
  `resolve_api_key`/`resolve_dsn` read the named variable at call time
  and return it without storing it back on the model, so a config
  object can be freely logged, dumped to YAML, or embedded in a
  snapshot header without ever leaking a credential (section 35.1:
  "Web stored config `api_key_env` değil encrypted credential ref" --
  v1's local/CLI config is the env-var-name form this module
  implements; the encrypted-credential-ref form is a later-release,
  server-side concern).
- Validate identifiers (Qdrant collection name; pgvector schema, table,
  column, and `where`-clause column names) against a bounded, safe
  pattern before any connector ever builds a request or query from
  them, and reject anything that looks like an attempt to smuggle a
  raw SQL fragment or operator through the `where` mapping (FR-111:
  "Operators/raw fragments yok").
- Validate `payload_mapping`/`mapping` values are non-empty when
  present, so a silently-empty mapping entry does not later produce a
  connector that resolves every point's `payload_projection` field to
  ``None`` without anyone noticing at config time.

`run_preflight` implements the preflight checks: reachability, auth,
and (when the caller supplies the
manifest's expected embedding dimension) an embedding-dimension-vs-
collection-dimension check, which is exactly the input
reconciliation's `EMBEDDING_DIMENSION_MISMATCH` finding (section 9)
needs -- computing that finding itself is a later release's job,
not this connector layer's.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from ragledger.connectors.base import (
    ConnectorConfigError,
    TargetSchema,
    VectorTargetConnector,
)
from ragledger.core.models import RagledgerModel

__all__ = [
    "PgvectorMapping",
    "PgvectorTargetConfig",
    "PreflightResult",
    "QdrantPayloadMapping",
    "QdrantSnapshotConfig",
    "QdrantTargetConfig",
    "resolve_env_credential",
    "run_preflight",
]

# A conservative, bounded safe-identifier pattern: this is intentionally
# stricter than what Qdrant collection names or PostgreSQL identifiers
# actually allow, trading a little flexibility for a config-time
# rejection of anything that could plausibly be an injection attempt.
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_PAYLOAD_PATH_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(value: str, *, field_name: str) -> str:
    if not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"{field_name} {value!r} is not a valid identifier "
            f"(expected {_IDENTIFIER_PATTERN.pattern!r})"
        )
    return value


def _validate_payload_path(value: str, *, field_name: str) -> str:
    if not _PAYLOAD_PATH_PATTERN.match(value):
        raise ValueError(
            f"{field_name} {value!r} is not a valid dotted payload path "
            f"(expected {_PAYLOAD_PATH_PATTERN.pattern!r})"
        )
    return value


def resolve_env_credential(env_var_name: str, *, env: Mapping[str, str] | None = None) -> str:
    """Resolve a credential from the named environment variable.

    Never logs, echoes, or otherwise persists the resolved value; the
    caller is responsible for the same discipline. Raises
    `ConnectorConfigError` (not `KeyError`) with a message that names
    the *variable*, never any value, when the variable is unset or
    empty.
    """
    if not _ENV_VAR_NAME_PATTERN.match(env_var_name):
        raise ConnectorConfigError(f"not a valid environment variable name: {env_var_name!r}")
    source = env if env is not None else os.environ
    value = source.get(env_var_name)
    if not value:
        raise ConnectorConfigError(f"environment variable {env_var_name!r} is not set")
    return value


# --------------------------------------------------------------------------
# Qdrant target config (section 35.1)
# --------------------------------------------------------------------------


class QdrantPayloadMapping(RagledgerModel):
    """Dotted payload-path mapping from logical identity fields to Qdrant payload fields."""

    source_id: str | None = None
    source_version_id: str | None = None
    chunk_id: str | None = None
    embedding_id: str | None = None
    tenant: str | None = None
    acl: str | None = None

    @field_validator("source_id", "source_version_id", "chunk_id", "embedding_id", "tenant", "acl")
    @classmethod
    def _validate_path(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _validate_payload_path(value, field_name=info.field_name or "payload_mapping field")


class QdrantSnapshotConfig(RagledgerModel):
    """The `snapshot:` block of a Qdrant target config."""

    include_vectors: bool = False
    page_size: int = Field(default=256, ge=1, le=10_000)


class QdrantTargetConfig(RagledgerModel):
    """A Qdrant target configuration, per the design specification section 35.1.

    `endpoint` must be a plain ``http``/``https`` URL with no embedded
    userinfo (``user:pass@host``) and no query string -- credentials
    and connection parameters belong in `api_key_env` and the
    connector's own request construction, never smuggled into the
    endpoint string itself.
    """

    type: Literal["qdrant"] = "qdrant"
    endpoint: str = Field(min_length=1)
    collection: str = Field(min_length=1, max_length=255)
    api_key_env: str | None = None
    vector_name: str | None = None
    payload_mapping: QdrantPayloadMapping = Field(default_factory=QdrantPayloadMapping)
    snapshot: QdrantSnapshotConfig = Field(default_factory=QdrantSnapshotConfig)
    embedding_dimension: int | None = Field(default=None, ge=1)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)

    model_config = ConfigDict(extra="forbid")

    @field_validator("collection")
    @classmethod
    def _validate_collection(cls, value: str) -> str:
        return _validate_identifier(value, field_name="collection")

    @field_validator("api_key_env")
    @classmethod
    def _validate_api_key_env(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _ENV_VAR_NAME_PATTERN.match(value):
            raise ValueError(f"api_key_env {value!r} is not a valid environment variable name")
        return value

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"endpoint scheme must be http or https, got {parts.scheme!r}")
        if not parts.hostname:
            raise ValueError("endpoint must include a hostname")
        if parts.username or parts.password:
            raise ValueError("endpoint must not embed credentials (no user:pass@host)")
        if parts.query:
            raise ValueError("endpoint must not include a query string")
        if parts.fragment:
            raise ValueError("endpoint must not include a fragment")
        # Normalize away a trailing slash so path joins downstream are
        # unambiguous ("https://x/" and "https://x" are the same target).
        normalized_path = parts.path.rstrip("/")
        return f"{parts.scheme}://{parts.netloc}{normalized_path}"

    def resolve_api_key(self, *, env: Mapping[str, str] | None = None) -> str | None:
        """Return the API key from `api_key_env`, or None if not configured."""
        if self.api_key_env is None:
            return None
        return resolve_env_credential(self.api_key_env, env=env)


# --------------------------------------------------------------------------
# pgvector target config (section 35.2)
# --------------------------------------------------------------------------


class PgvectorMapping(RagledgerModel):
    """Column-name mapping from logical identity fields to pgvector table columns."""

    source_id: str | None = None
    source_version_id: str | None = None
    chunk_id: str | None = None
    embedding_id: str | None = None
    tenant: str | None = None
    acl: str | None = None

    @field_validator("source_id", "source_version_id", "chunk_id", "embedding_id", "tenant", "acl")
    @classmethod
    def _validate_column(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name=info.field_name or "mapping field")

    def configured_items(self) -> list[tuple[str, str]]:
        """Return (logical_name, column_name) pairs for every mapped field."""
        return [
            (name, column)
            for name, column in (
                ("source_id", self.source_id),
                ("source_version_id", self.source_version_id),
                ("chunk_id", self.chunk_id),
                ("embedding_id", self.embedding_id),
                ("tenant", self.tenant),
                ("acl", self.acl),
            )
            if column is not None
        ]


WhereValue = str | int | float | bool | list[str] | list[int] | list[float]


class PgvectorTargetConfig(RagledgerModel):
    """A pgvector target configuration, per the design specification section 35.2.

    `where` is restricted to configured, validated column names with
    scalar or list values, generating a parameterized equality/`IN`
    predicate; there is no way to express an operator or a raw SQL
    fragment through this config (FR-111).
    """

    type: Literal["pgvector"] = "pgvector"
    dsn_env: str = Field(min_length=1)
    schema_name: str = Field(default="public", alias="schema")
    table: str
    primary_key: list[str] = Field(min_length=1)
    vector_column: str
    mapping: PgvectorMapping = Field(default_factory=PgvectorMapping)
    where: dict[str, WhereValue] = Field(default_factory=dict)
    fetch_size: int = Field(default=1000, ge=1, le=100_000)
    consistency: Literal["repeatable_read", "best_effort_paged"] = "repeatable_read"
    statement_timeout_ms: int = Field(default=30_000, ge=1, le=3_600_000)
    embedding_dimension: int | None = Field(default=None, ge=1)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    max_connect_retries: int = Field(default=3, ge=0, le=10)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("dsn_env")
    @classmethod
    def _validate_dsn_env(cls, value: str) -> str:
        if not _ENV_VAR_NAME_PATTERN.match(value):
            raise ValueError(f"dsn_env {value!r} is not a valid environment variable name")
        return value

    @field_validator("schema_name", "table", "vector_column")
    @classmethod
    def _validate_sql_identifier(cls, value: str, info: ValidationInfo) -> str:
        return _validate_identifier(value, field_name=info.field_name or "identifier")

    @field_validator("primary_key")
    @classmethod
    def _validate_primary_key(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("primary_key must list at least one column")
        return [_validate_identifier(column, field_name="primary_key") for column in value]

    @field_validator("where")
    @classmethod
    def _validate_where(cls, value: dict[str, WhereValue]) -> dict[str, WhereValue]:
        validated: dict[str, WhereValue] = {}
        for column, filter_value in value.items():
            _validate_identifier(column, field_name="where column")
            if isinstance(filter_value, list) and not filter_value:
                raise ValueError(f"where[{column!r}] list value must not be empty")
            validated[column] = filter_value
        return validated

    @model_validator(mode="after")
    def _validate_primary_key_not_vector_column(self) -> PgvectorTargetConfig:
        if self.vector_column in self.primary_key:
            raise ValueError("vector_column must not also be a primary_key column")
        return self

    def resolve_dsn(self, *, env: Mapping[str, str] | None = None) -> str:
        """Return the connection string from `dsn_env`."""
        return resolve_env_credential(self.dsn_env, env=env)


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightResult:
    """The outcome of `run_preflight`."""

    reachable: bool
    auth_ok: bool
    schema: TargetSchema | None
    expected_dimension: int | None
    observed_dimension: int | None
    dimension_match: bool | None
    message: str


def run_preflight(
    connector: VectorTargetConnector[object],
    *,
    expected_dimension: int | None = None,
    vector_name: str | None = None,
) -> PreflightResult:
    """Run reachability, auth, and embedding-dimension preflight checks against a connector.

    Calls `validate_configuration`, `test_connection`, and (only if
    both of those succeed) `inspect_target_schema`, comparing the
    resolved vector field's dimension against ``expected_dimension``
    when one is supplied. Never raises for a reachability/auth
    failure -- that outcome is reported in the returned
    `PreflightResult` -- but configuration errors from
    `validate_configuration` (a programmer/config mistake, not a live
    target condition) still propagate as `ConnectorConfigError`.
    """
    connector.validate_configuration()
    connection = connector.test_connection()
    if not connection.ok:
        return PreflightResult(
            reachable=False,
            auth_ok=False,
            schema=None,
            expected_dimension=expected_dimension,
            observed_dimension=None,
            dimension_match=None,
            message=connection.message,
        )

    schema = connector.inspect_target_schema()
    observed_dimension: int | None = None
    dimension_match: bool | None = None
    message = connection.message
    field_schema = schema.vector_field(vector_name)
    if field_schema is not None:
        observed_dimension = field_schema.dimension
    if expected_dimension is not None:
        if observed_dimension is None:
            dimension_match = False
            message = (
                f"{message}; expected embedding dimension {expected_dimension} but the "
                "target's vector field could not be resolved"
            )
        else:
            dimension_match = observed_dimension == expected_dimension
            if not dimension_match:
                message = (
                    f"{message}; embedding dimension mismatch: expected "
                    f"{expected_dimension}, target reports {observed_dimension}"
                )

    return PreflightResult(
        reachable=True,
        auth_ok=True,
        schema=schema,
        expected_dimension=expected_dimension,
        observed_dimension=observed_dimension,
        dimension_match=dimension_match,
        message=message,
    )
