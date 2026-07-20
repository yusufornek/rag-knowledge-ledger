"""Vector target connector interface, per PROJECT_SPEC.md section 13.1-13.2.

This module owns three things every connector implementation shares:

- `NormalizedPoint`: the vendor-neutral shape a connector reduces an
  observed index point to (section 13.2's exact field list). This is
  the only representation reconciliation (a later milestone) and the
  NDJSON snapshot format ever see; no connector-specific raw record
  type leaks past `normalize_point`.
- `VectorTargetConnector`: the abstract read-only interface (section
  13.1) every connector implements. Its method set is deliberately
  exhaustive: `validate_configuration`, `test_connection`,
  `inspect_target_schema`, `iterate_points`, `normalize_point`,
  `estimate_count`, `close`, plus a capabilities probe and a
  consistency-info accessor the milestone's task explicitly calls for.
  There is no mutation method anywhere on this class, and none may be
  added -- per section 13.1, "Mutation metodu interface'te yoktur"
  (there is no mutation method in the interface) and section 42.2's
  connector mutation guard is a runtime backstop for the same
  invariant, not a replacement for it.
- Shared value types (`ConsistencyInfo`, `TargetSchema`,
  `ConnectorCapabilities`, `ConnectionTestResult`, and the
  `Checkpoint`/`CheckpointToken` alias) that describe a target's shape
  and a snapshot pass's consistency outcome without committing to any
  one vendor's wire format.

Every timestamp field here is caller-supplied (`observed_at`,
`started_at`/`finished_at` on the NDJSON header/trailer in
`ragledger.connectors.ndjson`), never read from the wall clock inside
this module, so that connector code stays testable with an injected
clock -- see each connector's `clock` constructor parameter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import Field

from ragledger.core.canonical import JSONValue
from ragledger.core.hashing import hash_canonical
from ragledger.core.models import PointId, RagledgerModel, UtcDateTime

__all__ = [
    "Checkpoint",
    "ConnectionTestResult",
    "ConnectorCapabilities",
    "ConnectorConfigError",
    "ConnectorConnectionError",
    "ConnectorError",
    "ConnectorMutationBlockedError",
    "ConsistencyInfo",
    "ConsistencyMode",
    "NormalizedPoint",
    "SnapshotCompleteness",
    "TargetSchema",
    "VectorFieldSchema",
    "VectorTargetConnector",
    "apply_projection",
    "compute_payload_hash",
    "hash_vector",
]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ConnectorError(Exception):
    """Base class for every error a connector in this package raises."""


class ConnectorConfigError(ConnectorError):
    """Target configuration is invalid, incomplete, or fails validation."""


class ConnectorConnectionError(ConnectorError):
    """The target could not be reached, or reachability/auth checks failed."""


class ConnectorMutationBlockedError(ConnectorError):
    """A connector's transport-level read-only guard blocked a non-read operation.

    Raised by the section 42.2 mutation guard (the Qdrant httpx request
    event hook and the pgvector statement whitelist) whenever code --
    connector-internal or, in a test, deliberately adversarial -- tries
    to issue anything other than the small, explicit set of read
    operations the connector is allowed to perform. This is a defense
    in depth backstop: `VectorTargetConnector` exposes no method that
    could construct a mutating request in the first place.
    """


# --------------------------------------------------------------------------
# Normalized point (section 13.2)
# --------------------------------------------------------------------------


class NormalizedPoint(RagledgerModel):
    """A single observed index point, reduced to section 13.2's exact field list.

    Field-by-field mapping to the spec's list:

    - `target_id` / `scope`: which target and which collection/table
      the point was observed in.
    - `point_id`: the typed canonical JSON point identifier (FR-104's
      Qdrant string/number preservation, FR-115's composite pgvector
      primary key as a canonical JSON object).
    - `vector_names` / `vector_dimensions`: which named vectors this
      point carries and their dimensionality, always populated from
      target-schema metadata (known even when the raw vector was never
      fetched).
    - `vector_hashes`: optional, only present when `include_vectors`
      was requested of `iterate_points` and the raw vector was
      actually returned by the target; a SHA-256 over the RFC 8785
      canonical JSON array of the vector's float components (see
      `hash_vector`), never the raw floats themselves, per the
      "hash vectors, do not require storing raw vectors" instruction.
    - `payload_projection` / `payload_hash`: the target-config mapped
      identity/tenant/ACL fields that were actually resolved from the
      raw payload, keyed by their canonical logical name (`source_id`,
      `source_version_id`, `chunk_id`, `embedding_id`, `tenant`,
      `acl`) rather than by the vendor-specific payload path they came
      from -- this is what FR-096's "raw payload retention policy;
      default selected mapped fields only" means in practice: nothing
      beyond these mapped fields is retained. `payload_hash` is
      `hash_canonical(payload_projection)`, used by reconciliation for
      `PAYLOAD_DRIFT` detection.
    - `source_id` / `source_version_id` / `chunk_id` / `embedding_id`:
      convenience top-level copies of the corresponding
      `payload_projection` entries (when present), for reconciliation's
      section 9.1 matching order to key off of directly without
      re-parsing the projection.
    - `acl` / `tenant`: convenience top-level copies, same rationale.
    - `observed_at`: when this connector actually read the point (not
      when the underlying source/chunk/embedding was produced).
    - `raw_locator`: a vendor-specific but credential-free locator for
      this point (for example ``qdrant:support_kb#42`` or
      ``pgvector:public.document_chunks#{"id":42}``) -- never a full
      connection URL, never an API key or DSN.
    - `normalization_warnings`: bounded, stable warning codes recorded
      when a mapped field could not be resolved, a vector was
      requested but missing, or a payload value had an unexpected
      shape; normalization always produces a point rather than raising,
      so a single malformed point never aborts an entire snapshot.
    """

    target_id: str
    scope: str
    point_id: PointId
    vector_names: list[str] = Field(default_factory=list)
    vector_dimensions: dict[str, int] = Field(default_factory=dict)
    vector_hashes: dict[str, str] | None = None
    payload_projection: dict[str, Any]
    payload_hash: str
    source_id: str | None = None
    source_version_id: str | None = None
    chunk_id: str | None = None
    embedding_id: str | None = None
    acl: list[str] | None = None
    tenant: str | None = None
    observed_at: UtcDateTime
    raw_locator: str
    normalization_warnings: list[str] = Field(default_factory=list)


def hash_vector(components: Sequence[float]) -> str:
    """Return the SHA-256 hex digest of a vector's RFC 8785 canonical JSON array.

    Used for `NormalizedPoint.vector_hashes`. Hashing the canonical
    encoding (rather than raw IEEE-754 bytes) means the same logical
    vector hashes identically regardless of which language or library
    produced it, matching the rest of this codebase's canonicalization
    convention (`ragledger.core.canonical`, `ragledger.core.hashing`).
    """
    values: list[JSONValue] = [float(component) for component in components]
    return hash_canonical(values)


def compute_payload_hash(payload_projection: Mapping[str, Any]) -> str:
    """Return `hash_canonical` of a normalized point's payload projection."""
    return hash_canonical(dict(payload_projection))


_PROJECTABLE_FIELDS = (
    "source_id",
    "source_version_id",
    "chunk_id",
    "embedding_id",
    "tenant",
    "acl",
)


def apply_projection(point: NormalizedPoint, projection: Sequence[str] | None) -> NormalizedPoint:
    """Restrict a normalized point to a caller-requested subset of mapped logical fields.

    ``projection`` names the logical fields (``source_id``, ``tenant``,
    and so on -- the same vocabulary as `NormalizedPoint.payload_projection`
    keys) `VectorTargetConnector.iterate_points` should resolve;
    ``None`` means every configured mapping, and `point` is returned
    unchanged. `payload_hash` is recomputed over the restricted
    projection so it always matches what `payload_projection` actually
    contains after filtering.
    """
    if projection is None:
        return point
    allowed = set(projection)
    restricted = {key: value for key, value in point.payload_projection.items() if key in allowed}
    updates: dict[str, Any] = {
        "payload_projection": restricted,
        "payload_hash": compute_payload_hash(restricted),
    }
    for field_name in _PROJECTABLE_FIELDS:
        if field_name not in allowed:
            updates[field_name] = None
    return point.model_copy(update=updates)


# --------------------------------------------------------------------------
# Checkpoints
# --------------------------------------------------------------------------

Checkpoint = JSONValue
"""An opaque, JSON-serializable resume token.

Qdrant connectors encode it as the last-returned `next_page_offset`
(a point id); pgvector connectors encode it as a canonical JSON object
of the last-observed primary key column values. Callers persist
whatever `Checkpoint` value they see between points and pass it back
to `VectorTargetConnector.iterate_points` to resume; this package never
interprets a checkpoint's shape itself.
"""


# --------------------------------------------------------------------------
# Consistency
# --------------------------------------------------------------------------


class ConsistencyMode(StrEnum):
    """How a connector obtained ordering/isolation for one `iterate_points` pass."""

    STRICT_CONSISTENT = "strict_consistent"
    """A single database snapshot (for example pgvector's `REPEATABLE READ`
    transaction, section 13.4) backs the entire pass: no concurrent
    write can be observed as a partial or duplicated read."""

    BEST_EFFORT_PAGED = "best_effort_paged"
    """Each page (or row batch) is read independently, without holding
    one long-lived transaction/snapshot open; concurrent writes between
    pages are possible and are only detected, not prevented, via the
    start/end count probe recorded in `ConsistencyInfo`."""

    BEST_EFFORT_LIVE = "best_effort_live"
    """The target gives no point-in-time snapshot guarantee at all
    (section 13.3's Qdrant scroll case): iteration reflects whatever
    state the collection was in as each page was requested."""


class SnapshotCompleteness(StrEnum):
    """Whether a pass's before/after point-count probe detected drift."""

    COMPLETE = "complete"
    """Start and end counts agree with the number of points actually
    yielded; no concurrent mutation was detected during this pass."""

    INCOMPLETE = "incomplete"
    """A count mismatch was detected: points were added or removed
    from the target while this pass was iterating. Per section 13.3,
    a reconciliation run against an `INCOMPLETE` snapshot should be
    treated as provisional."""


@dataclass(frozen=True)
class ConsistencyInfo:
    """The consistency outcome of one `iterate_points` pass, per sections 13.3/13.4."""

    mode: ConsistencyMode
    completeness: SnapshotCompleteness
    start_count: int | None
    end_count: int | None
    observed_count: int
    detail: str | None = None


# --------------------------------------------------------------------------
# Target schema and capabilities
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VectorFieldSchema:
    """One named vector's dimensionality/distance, per FR-100."""

    name: str
    dimension: int
    distance: str | None = None


@dataclass(frozen=True)
class TargetSchema:
    """A target's collection/table schema, per FR-100/FR-113/section 35.4."""

    target_id: str
    scope: str
    vector_fields: tuple[VectorFieldSchema, ...]
    point_id_type: str
    payload_indexes: tuple[str, ...] = ()
    approx_point_count: int | None = None
    resolved_scope: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def vector_field(self, name: str | None) -> VectorFieldSchema | None:
        """Return the named vector field, or the sole field when ``name`` is None."""
        if name is not None:
            for candidate in self.vector_fields:
                if candidate.name == name:
                    return candidate
            return None
        return self.vector_fields[0] if len(self.vector_fields) == 1 else None


@dataclass(frozen=True)
class ConnectorCapabilities:
    """A connector's capability probe, per this milestone's task description."""

    target_type: str
    supports_resume: bool
    supports_vector_hash: bool
    supports_sampling: bool
    max_page_size: int | None
    consistency_modes: tuple[ConsistencyMode, ...]


@dataclass(frozen=True)
class ConnectionTestResult:
    """The outcome of `VectorTargetConnector.test_connection`."""

    ok: bool
    message: str
    latency_ms: float | None = None


# --------------------------------------------------------------------------
# Connector interface (section 13.1)
# --------------------------------------------------------------------------

RawRecordT = TypeVar("RawRecordT")


class VectorTargetConnector(ABC, Generic[RawRecordT]):
    """The read-only vector target connector interface, per PROJECT_SPEC.md section 13.1.

    Implementations: `ragledger.connectors.qdrant.QdrantConnector`,
    `ragledger.connectors.pgvector.PgvectorConnector`, and
    `ragledger.connectors.ndjson` (which reads/writes the snapshot
    format directly rather than implementing this live-target
    interface). Every implementation is a context manager: `close()` is
    always safe to call more than once and `__exit__` calls it.
    """

    @abstractmethod
    def validate_configuration(self) -> None:
        """Validate this connector's target configuration.

        Raises `ConnectorConfigError` with a specific, actionable
        message on the first problem found (bad identifier, missing
        required mapping, out-of-range page size, and so on). Never
        makes a network/database call; see `test_connection` for
        reachability.
        """

    @abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        """Probe reachability and authentication against the live target."""

    @abstractmethod
    def inspect_target_schema(self) -> TargetSchema:
        """Return the target's collection/table schema (vector config, indexes, counts)."""

    @abstractmethod
    def capabilities(self) -> ConnectorCapabilities:
        """Return this connector's capability probe (resume support, page size, and so on)."""

    @abstractmethod
    def estimate_count(self) -> int | None:
        """Return an approximate point/row count, or None if unavailable."""

    @abstractmethod
    def normalize_point(self, raw: RawRecordT, *, include_vectors: bool = False) -> NormalizedPoint:
        """Reduce one connector-specific raw record to a `NormalizedPoint`.

        Never raises for a malformed or partially mapped record: a
        field that cannot be resolved is simply omitted from
        `payload_projection`/left `None`, and a stable warning code is
        appended to `normalization_warnings` instead.
        """

    @abstractmethod
    def iterate_points(
        self,
        *,
        checkpoint: Checkpoint | None = None,
        projection: Sequence[str] | None = None,
        include_vectors: bool = False,
    ) -> Iterator[NormalizedPoint]:
        """Stream every point in the target exactly once (best effort), from ``checkpoint``.

        ``projection`` restricts which mapped logical fields
        (``source_id``, ``tenant``, and so on) are resolved and fetched
        where the underlying target supports column/field-level
        projection; ``None`` means "every configured mapping".
        ``include_vectors`` defaults to false per FR-102/FR-114: raw
        vectors are never fetched unless explicitly requested.

        After this generator is fully consumed, `get_consistency_info`
        reflects this pass's before/after drift probe.
        """

    @abstractmethod
    def get_consistency_info(self) -> ConsistencyInfo:
        """Return the consistency outcome of the most recently completed `iterate_points` pass.

        Raises `RuntimeError` if `iterate_points` has not yet been run
        to completion.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any open connection/transport. Idempotent."""

    def __enter__(self) -> VectorTargetConnector[RawRecordT]:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
