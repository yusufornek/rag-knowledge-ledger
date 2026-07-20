"""Pydantic v2 models for manifest v1, per PROJECT_SPEC.md sections 7 and 33.

These models mirror `docs/spec/manifest-v1.schema.json` field-for-field:
every model here has a `$defs` entry in that schema with the same
required/optional fields, enum values, and nesting. `ragledger.core.manifest`
is the authority on schema validation (it validates the dumped JSON
against the schema document itself); these models exist to give callers
a typed, IDE-checkable way to construct manifest content without hand
building dicts, and to make constraints like "required" and "one of
these five assertion types" impossible to violate by construction.

Unknown/unobserved convention (PROJECT_SPEC.md line 21: "Kaynağı
gözlenmeyen metadata `unknown` olur"): nothing in this codebase invents
a value for metadata that was not actually observed. Where the schema
defines an explicit `"unknown"` enum member (`IndexBinding.write_status`),
use it. For free-form optional string fields that have no such member
(for example `SourceRecord.discovered_by`), callers that cannot
determine a real value should write the literal string `"unknown"`
rather than guessing or omitting a value that downstream code will
otherwise treat as "not applicable". Fabricated values (a guessed parser
version, an invented license) are never acceptable, per PROJECT_SPEC.md
section 5's "do not let the ledger make up the truth" principle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.functional_serializers import PlainSerializer
from pydantic.functional_validators import AfterValidator

SHA256_PATTERN = r"^[a-f0-9]{64}$"
_BASE64URL_PATTERN = r"^[A-Za-z0-9_-]+$"

Id = Annotated[str, Field(min_length=1)]
Sha256Hash = Annotated[str, Field(pattern=SHA256_PATTERN)]


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError(
            "timestamps must be timezone-aware; naive datetimes are ambiguous and "
            "manifest determinism requires an explicit, unambiguous instant"
        )
    return value.astimezone(UTC)


def _serialize_utc(value: datetime) -> str:
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += f".{value.microsecond:06d}"
    return text + "Z"


UtcDateTime = Annotated[
    datetime,
    AfterValidator(_require_timezone_aware),
    PlainSerializer(_serialize_utc, return_type=str, when_used="json"),
]
"""A timestamp field that must be supplied explicitly by the caller.

There is no default and no fallback to wall-clock time anywhere in this
module: every timestamp in a manifest is data the caller passed in,
never `datetime.now()`, so that building the same manifest twice with
the same inputs (including the same explicit timestamps) is
byte-identical. See the "--reproducible" / `SOURCE_DATE_EPOCH` handling
in PROJECT_SPEC.md section 7.2.
"""


class RagledgerModel(BaseModel):
    """Base model: unknown fields are a hard error, matching `additionalProperties: false`."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Shared value objects
# --------------------------------------------------------------------------


class WarningRecord(RagledgerModel):
    """A stable, bounded warning code (PROJECT_SPEC.md: "Stable codes; bounded")."""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str | None = None
    context: dict[str, Any] | None = None


class StageRecord(RagledgerModel):
    """One pipeline stage's tool identity and input/output counts."""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    config_hash: Sha256Hash | None = None
    input_count: int | None = Field(default=None, ge=0)
    output_count: int | None = Field(default=None, ge=0)


class BuildEnvironment(RagledgerModel):
    """OS/image/interpreter/lockfile identity. Never a build host name."""

    os: str | None = None
    image_digest: str | None = None
    python_version: str = Field(min_length=1)
    package_lock_hash: Sha256Hash | None = None


BuildStatus = Literal["complete", "incomplete", "cancelled"]


class BuildRecord(RagledgerModel):
    """The manifest's `build` envelope field, per PROJECT_SPEC.md section 33.1."""

    build_id: Id
    status: BuildStatus
    source_snapshot_hash: Sha256Hash
    pipeline_config_hash: Sha256Hash
    started_at: UtcDateTime
    completed_at: UtcDateTime
    environment: BuildEnvironment
    stages: list[StageRecord] = Field(default_factory=list)
    warnings: list[WarningRecord] = Field(default_factory=list)
    extensions: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Source record
# --------------------------------------------------------------------------

SourceStatus = Literal["active", "tombstone"]
RelationshipType = Literal["duplicate_of", "supersedes", "renamed_from"]


class SourceRelationship(RagledgerModel):
    type: RelationshipType
    target_version_id: Id


class SourceRecord(RagledgerModel):
    """A source asset/version, per PROJECT_SPEC.md sections 7.3 and 33.2.

    `uri` is namespace-relative (for example ``file:documents/refund.pdf``),
    never an absolute local path, Windows drive letter, or UNC path.
    `modified_at` is informational only and never part of content identity.
    """

    id: Id
    version_id: Id
    namespace: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_hash: Sha256Hash
    modified_at: UtcDateTime | None = None
    discovered_by: str | None = None
    source_system: str = Field(min_length=1)
    status: SourceStatus
    declared_tenant: str | None = None
    declared_acl_assertion_id: Id | None = None
    license_assertion_ids: list[Id] = Field(default_factory=list)
    raw_artifact_ref: Id | None = None
    relationships: list[SourceRelationship] = Field(default_factory=list)
    extensions: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Parse record
# --------------------------------------------------------------------------

ParseStatus = Literal["success", "partial", "fail"]


class OcrInfo(RagledgerModel):
    enabled: bool
    engine: str | None = None
    model: str | None = None
    languages: list[str] | None = None


class ParseRecord(RagledgerModel):
    """A parse run, per PROJECT_SPEC.md sections 7.4 and 33.

    No machine path or secret is ever recorded here; `config_redacted` is
    a secret-free echo of the parser configuration actually used.
    """

    id: Id
    source_version_id: Id
    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    container_digest: str | None = None
    config_hash: Sha256Hash | None = None
    config_redacted: dict[str, Any] | None = None
    ocr: OcrInfo | None = None
    status: ParseStatus
    warnings: list[WarningRecord] = Field(default_factory=list)
    parsed_artifact_ref: Id
    duration_seconds: float = Field(ge=0)
    extensions: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Chunk record
# --------------------------------------------------------------------------


class StructuralLocator(RagledgerModel):
    """A typed structural locator, per PROJECT_SPEC.md section 33.3.

    Page numbers are 1-based and user-facing; the mapping to internal
    parser page indices is kept by the parser stage, not here. Character
    offsets are relative to normalized parsed text, never raw source
    bytes. `ordinal` disambiguates repeated locators (for example the
    same heading appearing twice in a document).
    """

    kind: str = Field(min_length=1)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    heading_path: list[str] | None = None
    element_ids: list[str] | None = None
    character_start: int | None = Field(default=None, ge=0)
    character_end: int | None = Field(default=None, ge=0)
    ordinal: int = Field(ge=0)


class Tokenizer(RagledgerModel):
    name: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class ChunkMetadata(RagledgerModel):
    """Non-reserved chunk metadata.

    The reserved keys listed in PROJECT_SPEC.md section 33.4
    (``ragledger.source_id``, ``source_version_id``, ``chunk_id``,
    ``embedding_id``, ``manifest_hash``, ``locator``, ``tenant``,
    ``acl_hash``, ``license_expression``, ``pii_status``) are populated
    by connector payload mapping, never written here; user-defined
    metadata belongs under `custom`.
    """

    heading_path: list[str] | None = None
    table_caption: str | None = None
    custom: dict[str, Any] | None = None


class ChunkRecord(RagledgerModel):
    """A chunk revision, per PROJECT_SPEC.md sections 7.5 and 33."""

    id: Id
    source_version_id: Id
    parse_run_id: Id
    locator: StructuralLocator
    raw_hash: Sha256Hash
    contextualized_hash: Sha256Hash
    token_count: int = Field(ge=0)
    tokenizer: Tokenizer
    text_artifact_ref: Id | None = None
    neighbor_ids: list[Id] = Field(default_factory=list)
    metadata: ChunkMetadata | None = None
    pii_assertion_ids: list[Id] = Field(default_factory=list)
    license_assertion_ids: list[Id] = Field(default_factory=list)
    acl_assertion_ids: list[Id] = Field(default_factory=list)
    extensions: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Embedding record
# --------------------------------------------------------------------------

Normalization = Literal["none", "l2"]
DistanceExpectation = Literal["cosine", "dot", "euclidean", "manhattan"]


class EmbeddingModelInfo(RagledgerModel):
    provider: str = Field(min_length=1)
    name: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class EmbeddingRecord(RagledgerModel):
    """An embedding revision, per PROJECT_SPEC.md sections 7.6 and 33.

    Raw vectors and API keys are never present here; `usage` is
    adapter-specific metadata such as input token count or batch size.
    """

    id: Id
    chunk_id: Id
    model: EmbeddingModelInfo
    dimension: int = Field(ge=1)
    dtype: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    normalization: Normalization = "none"
    distance_expectation: DistanceExpectation | None = None
    contextualized_hash: Sha256Hash
    vector_hash: Sha256Hash | None = None
    generated_at: UtcDateTime
    usage: dict[str, Any] | None = None
    extensions: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Index binding
# --------------------------------------------------------------------------

WriteStatus = Literal["pending", "written", "unknown"]
PointId = str | int | list[Any] | dict[str, Any]


class IndexBinding(RagledgerModel):
    """An expected index binding, per PROJECT_SPEC.md sections 7.7 and 33.

    `target` is an alias, never a connection URL or credential.
    `point_id` preserves Qdrant's string/number point ids (FR-104) and
    represents composite pgvector primary keys as canonical JSON
    (FR-115).
    """

    id: Id
    target: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    point_id: PointId
    embedding_id: Id
    expected_payload_hash: Sha256Hash
    expected_payload_projection: dict[str, Any] | None = None
    tenant_projection: Any | None = None
    acl_projection: Any | None = None
    write_status: WriteStatus | None = None
    write_receipt: dict[str, Any] | None = None
    extensions: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Artifact ref
# --------------------------------------------------------------------------

Compression = Literal["none", "gzip", "zstd"]
Encryption = Literal["none", "server_managed"]
Sensitivity = Literal["public", "internal", "sensitive", "restricted"]


class ArtifactRef(RagledgerModel):
    """A content-addressed artifact reference, per PROJECT_SPEC.md section 33.5.

    `locator` is a relative or logical object locator, never a signed
    URL or embedded credential.
    """

    artifact_id: Id
    media_type: str = Field(min_length=1)
    sha256: Sha256Hash
    size_bytes: int = Field(ge=0)
    compression: Compression
    encryption: Encryption
    locator: str = Field(min_length=1)
    sensitivity: Sensitivity


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------


class PiiScannerInfo(RagledgerModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    language: str | None = None
    config_hash: Sha256Hash | None = None


PiiScanStatus = Literal["no_findings_detected", "findings_detected"]


class PiiFinding(RagledgerModel):
    """A single PII finding. Never carries a raw PII value (FR-052)."""

    entity_type: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    masked_preview: str | None = None
    value_hmac: Sha256Hash | None = None
    recognizer_id: str = Field(min_length=1)
    recognizer_version: str = Field(min_length=1)


class PiiScanAssertion(RagledgerModel):
    """A `PII_SCAN` typed assertion.

    `status` of ``no_findings_detected`` is never reported as a
    guarantee of cleanliness (FR-055); it only records that this scan
    run found nothing.
    """

    id: Id
    type: Literal["PII_SCAN"] = "PII_SCAN"
    subject_ref: Id
    created_at: UtcDateTime
    scanner: PiiScannerInfo
    status: PiiScanStatus
    findings: list[PiiFinding] = Field(default_factory=list)
    extensions: dict[str, Any] | None = None


LicenseMethod = Literal[
    "user_assertion", "frontmatter", "sidecar", "path_rule", "repository_default"
]


class LicenseAssertion(RagledgerModel):
    """A `LICENSE` typed assertion: an SPDX expression, or the literal ``NOASSERTION``."""

    id: Id
    type: Literal["LICENSE"] = "LICENSE"
    subject_ref: Id
    created_at: UtcDateTime
    spdx_expression: str = Field(min_length=1)
    method: LicenseMethod
    confidence: float | None = Field(default=None, ge=0, le=1)
    license_list_version: str | None = None
    conflicting_assertion_ids: list[Id] = Field(default_factory=list)
    extensions: dict[str, Any] | None = None


class AclAssertion(RagledgerModel):
    """An `ACL` typed assertion: a canonical set of typed principal entries.

    Deny entries are not supported in v1.
    """

    id: Id
    type: Literal["ACL"] = "ACL"
    subject_ref: Id
    created_at: UtcDateTime
    acl_hash: Sha256Hash
    entries: list[str] = Field(default_factory=list)
    extensions: dict[str, Any] | None = None


class TenantAssertion(RagledgerModel):
    """A `TENANT` typed assertion: a single tenant key/value pair and its hash."""

    id: Id
    type: Literal["TENANT"] = "TENANT"
    subject_ref: Id
    created_at: UtcDateTime
    tenant_hash: Sha256Hash
    tenant_key: str = Field(min_length=1)
    tenant_value: str = Field(min_length=1)
    extensions: dict[str, Any] | None = None


class QualityAssertion(RagledgerModel):
    """A `QUALITY` typed assertion: bounded parser/chunk warnings for one subject."""

    id: Id
    type: Literal["QUALITY"] = "QUALITY"
    subject_ref: Id
    created_at: UtcDateTime
    warnings: list[WarningRecord] = Field(default_factory=list)
    extensions: dict[str, Any] | None = None


Assertion = Annotated[
    PiiScanAssertion | LicenseAssertion | AclAssertion | TenantAssertion | QualityAssertion,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Statistics, integrity, signatures
# --------------------------------------------------------------------------


class Statistics(RagledgerModel):
    source_count: int = Field(ge=0)
    source_version_count: int | None = Field(default=None, ge=0)
    parse_run_count: int | None = Field(default=None, ge=0)
    chunk_count: int = Field(ge=0)
    embedding_count: int = Field(ge=0)
    index_binding_count: int = Field(ge=0)
    assertion_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    warning_count: int | None = Field(default=None, ge=0)
    extensions: dict[str, Any] | None = None


class Integrity(RagledgerModel):
    canonicalization: Literal["RFC8785"] = "RFC8785"
    hash_algorithm: Literal["sha256"] = "sha256"
    manifest_hash: Sha256Hash


class SignatureRecord(RagledgerModel):
    """A detached-form Ed25519 signature over a manifest's signing view.

    `key_id` is the SHA-256 fingerprint of the signer's Ed25519 public
    key; the private key is never present in a manifest. `signature` is
    base64url-encoded with no padding. See `ragledger.core.signing`.
    """

    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Sha256Hash
    signature: str = Field(pattern=_BASE64URL_PATTERN, min_length=1)
    signed_at: UtcDateTime
    issuer: str | None = None


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


class ManifestEnvelope(RagledgerModel):
    """The manifest v1 envelope, per PROJECT_SPEC.md section 7.1.

    Construct instances through `ragledger.core.manifest.build_manifest`
    rather than directly: it also computes `statistics` and
    `integrity.manifest_hash` for you, which are not meant to be
    supplied by hand.
    """

    schema_: Literal["https://ragledger.dev/schemas/manifest-v1.json"] = Field(
        default="https://ragledger.dev/schemas/manifest-v1.json", alias="schema"
    )
    media_type: Literal["application/vnd.ragledger.manifest.v1+json"] = (
        "application/vnd.ragledger.manifest.v1+json"
    )
    manifest_version: Literal["1.0"] = "1.0"
    created_at: UtcDateTime
    ledger_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    namespace: str = Field(min_length=1)
    build: BuildRecord
    sources: list[SourceRecord] = Field(default_factory=list)
    parse_runs: list[ParseRecord] = Field(default_factory=list)
    chunks: list[ChunkRecord] = Field(default_factory=list)
    embeddings: list[EmbeddingRecord] = Field(default_factory=list)
    index_bindings: list[IndexBinding] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    statistics: Statistics
    integrity: Integrity
    signatures: list[SignatureRecord] = Field(default_factory=list)
    extensions: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
