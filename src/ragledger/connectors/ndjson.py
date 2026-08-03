"""The NDJSON snapshot format: writer, reader, and the NDJSON connector, per section 13.5.

A snapshot file is zstd-compressed newline-delimited JSON
(``.ndjson.zst``):

- Line 1 is a header record (`SnapshotHeader`): target identity,
  vector schema, when the pass started, the connector version, the
  consistency mode it was run under, and -- when this is a sample
  rather than a full snapshot -- the explicit method/seed/rate FR-093
  requires.
- Lines 2..N-1 are `NormalizedPoint` records, one per line, in
  whatever order the source connector produced them.
- The last line is a trailer record (`SnapshotTrailer`): when the pass
  finished, the point count, the consistency completeness/counts the
  pass observed, and `content_hash` -- the SHA-256 of the concatenated
  canonical bytes of every point line (not the header or trailer
  themselves), which is what makes a snapshot file's content
  independently verifiable and, per FR-097, treated as immutable: any
  edit to a single point line changes `content_hash`, so
  `SnapshotReader` always re-derives it while streaming and raises
  `SnapshotIntegrityError` the moment it disagrees with the trailer's
  claimed value.

Every record is written and read as RFC 8785 canonical JSON
(`ragledger.core.canonical.canonical_bytes`), the same canonicalization
the rest of this codebase uses for content-addressed hashing, so two
snapshots built from the same points in the same order are
byte-identical.

Both directions stream: `write_snapshot` consumes an `Iterable` of
points one at a time and never buffers more than the current point
plus a running SHA-256 hasher's fixed internal state; `SnapshotReader.points`
is a generator that never materializes the file's points as a list.
This is what makes both sides safe to use for snapshots far too large
to hold in memory, and is also section 13.5's rationale for this
format existing at all: "vendor-independent CI fixture ve air-gapped
kullanım sağlar" (a vendor-independent CI fixture, and offline/
air-gapped use).

`NdjsonConnector` implements `VectorTargetConnector` by reading an
already-written snapshot file back as if it were a live target: this
is what lets reconciliation (a later release) and this release's
own tests treat a committed NDJSON fixture exactly like a live Qdrant
or pgvector connection, with no target-specific code path.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import zstandard
from pydantic import Field, ValidationError

from ragledger.connectors.base import (
    Checkpoint,
    ConnectionTestResult,
    ConnectorCapabilities,
    ConnectorConfigError,
    ConnectorConnectionError,
    ConnectorError,
    ConsistencyInfo,
    ConsistencyMode,
    NormalizedPoint,
    SnapshotCompleteness,
    TargetSchema,
    VectorFieldSchema,
    VectorTargetConnector,
    apply_projection,
)
from ragledger.core.canonical import canonical_bytes
from ragledger.core.models import SHA256_PATTERN, PointId, RagledgerModel, UtcDateTime

__all__ = [
    "NDJSON_SCHEMA_VERSION",
    "NdjsonConnector",
    "SnapshotHeader",
    "SnapshotIntegrityError",
    "SnapshotReader",
    "SnapshotTrailer",
    "write_snapshot",
]

NDJSON_SCHEMA_VERSION = "1"

DEFAULT_COMPRESSION_LEVEL = 3


class SnapshotIntegrityError(ConnectorError):
    """Raised when a snapshot file fails schema, count, or content-hash verification."""


# --------------------------------------------------------------------------
# Header / trailer records
# --------------------------------------------------------------------------


class SnapshotHeader(RagledgerModel):
    """A snapshot's first line, per section 13.5 and FR-094/FR-093."""

    record_type: Literal["header"] = "header"
    schema_version: str = NDJSON_SCHEMA_VERSION
    target_id: str
    scope: str
    target_type: str
    vector_names: list[str] = Field(default_factory=list)
    vector_dimensions: dict[str, int] = Field(default_factory=dict)
    started_at: UtcDateTime
    connector_version: str
    consistency_mode: str
    scope_filter: dict[str, Any] | None = None
    snapshot_kind: Literal["full", "sample"] = "full"
    sample_method: str | None = None
    sample_seed: int | None = None
    sample_rate: float | None = Field(default=None, ge=0, le=1)


class SnapshotTrailer(RagledgerModel):
    """A snapshot's last line, per section 13.5 and FR-097."""

    record_type: Literal["trailer"] = "trailer"
    finished_at: UtcDateTime
    point_count: int = Field(ge=0)
    consistency_completeness: str
    consistency_start_count: int | None = None
    consistency_end_count: int | None = None
    content_hash: str = Field(pattern=SHA256_PATTERN)


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------


def write_snapshot(
    path: Path,
    header: SnapshotHeader,
    points: Iterable[NormalizedPoint],
    *,
    finished_at: datetime,
    consistency_provider: Callable[[], ConsistencyInfo] | None = None,
    compression_level: int = DEFAULT_COMPRESSION_LEVEL,
) -> SnapshotTrailer:
    """Stream ``points`` to ``path`` as a zstd-compressed NDJSON snapshot.

    ``points`` is consumed exactly once, in order, one point at a
    time; nothing here ever collects it into a list. ``consistency_provider``,
    when given, is called only after every point has been written --
    the natural way to pass a connector's bound `get_consistency_info`
    method, which is only valid to call once its `iterate_points` pass
    has fully completed. Returns the `SnapshotTrailer` that was
    written, so a caller can inspect `content_hash`/`point_count`
    without re-reading the file.
    """
    hasher = hashlib.sha256()
    count = 0
    # `write_checksum=True` asks zstd for its own frame content
    # checksum, so single-bit/byte corruption of the compressed file on
    # disk is caught as a `zstandard.ZstdError` at decompression time --
    # a first line of defense in front of this format's own
    # `content_hash` trailer field, which additionally catches
    # corruption or tampering that happens to still decompress cleanly
    # (for example, a valid but hand-edited point line).
    compressor = zstandard.ZstdCompressor(level=compression_level, write_checksum=True)
    path = Path(path)
    with path.open("wb") as raw_file, compressor.stream_writer(raw_file) as writer:
        writer.write(canonical_bytes(header.model_dump(mode="json", exclude_none=True)))
        writer.write(b"\n")
        for point in points:
            line_bytes = canonical_bytes(point.model_dump(mode="json", exclude_none=True))
            hasher.update(line_bytes)
            hasher.update(b"\n")
            writer.write(line_bytes)
            writer.write(b"\n")
            count += 1

        consistency = consistency_provider() if consistency_provider is not None else None
        trailer = SnapshotTrailer(
            finished_at=finished_at,
            point_count=count,
            consistency_completeness=(
                consistency.completeness.value
                if consistency is not None
                else SnapshotCompleteness.COMPLETE.value
            ),
            consistency_start_count=consistency.start_count if consistency is not None else None,
            consistency_end_count=consistency.end_count if consistency is not None else None,
            content_hash=hasher.hexdigest(),
        )
        writer.write(canonical_bytes(trailer.model_dump(mode="json", exclude_none=True)))
        writer.write(b"\n")
    return trailer


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------


def _parse_header(line: str) -> SnapshotHeader:
    try:
        return SnapshotHeader.model_validate_json(line)
    except ValidationError as exc:
        raise SnapshotIntegrityError(f"invalid snapshot header: {exc}") from exc


def _parse_trailer(line: str) -> SnapshotTrailer:
    try:
        return SnapshotTrailer.model_validate_json(line)
    except ValidationError as exc:
        raise SnapshotIntegrityError(f"invalid snapshot trailer: {exc}") from exc


def _parse_point(line: str) -> NormalizedPoint:
    try:
        return NormalizedPoint.model_validate_json(line)
    except ValidationError as exc:
        raise SnapshotIntegrityError(f"invalid snapshot point record: {exc}") from exc


def _point_id_key(point_id: PointId) -> str:
    """Return a canonical-JSON string key for a point id of any typed shape.

    Used both for `SnapshotReader.points`'s duplicate-id check and for
    `NdjsonConnector`'s checkpoint matching, so both compare point ids
    the same deterministic way regardless of whether the id is a
    string, an integer, or (per FR-115) a composite-key JSON object.
    """
    return canonical_bytes(point_id).decode("utf-8")


class SnapshotReader:
    """Streams a `.ndjson.zst` snapshot back: header eagerly, points and trailer lazily.

    `header` is available immediately after construction. `points()`
    is a one-shot generator (streaming, bounded memory) that must be
    fully consumed before `trailer` becomes available -- the trailer
    is the file's last line, and content-hash/count verification
    against it can only happen once every point line has actually been
    read. Raises `SnapshotIntegrityError` the moment a corrupt frame,
    a schema-invalid record, a missing trailer, a point-count mismatch,
    or a content-hash mismatch is detected.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._file = self._path.open("rb")
        try:
            decompressor = zstandard.ZstdDecompressor()
            raw_stream = decompressor.stream_reader(self._file)
            self._text_stream = io.TextIOWrapper(
                io.BufferedReader(raw_stream), encoding="utf-8", newline="\n"
            )
            first_line = self._read_line()
        except Exception:
            self._file.close()
            raise
        if not first_line:
            self.close()
            raise SnapshotIntegrityError(f"empty snapshot, missing header: {self._path}")
        self._header = _parse_header(first_line)
        self._pending_line: str | None = self._read_line()
        self._trailer: SnapshotTrailer | None = None
        self._hasher = hashlib.sha256()
        self._point_count = 0
        self._consumed = False

    @property
    def header(self) -> SnapshotHeader:
        return self._header

    @property
    def trailer(self) -> SnapshotTrailer:
        if self._trailer is None:
            raise RuntimeError("trailer is only available after points() is fully consumed")
        return self._trailer

    def points(self, *, check_duplicates: bool = False) -> Iterator[NormalizedPoint]:
        """Yield every point line, verifying the trailer once exhausted.

        ``check_duplicates`` opts into an in-memory (scope, point_id)
        set for small fixtures/CI use; it is bounded by the number of
        points in the file, which is exactly the memory trade-off it
        is not safe to make for very large production snapshots --
        that scale of duplicate detection is reconciliation's
        streaming/external-sort job (section 8.13, FR-121), not this
        format reader's.
        """
        if self._consumed:
            raise RuntimeError("points() has already been consumed for this reader")
        self._consumed = True
        seen: set[tuple[str, str]] | None = set() if check_duplicates else None
        while self._pending_line is not None:
            following = self._read_line()
            if following is None:
                self._finalize(self._pending_line)
                self._pending_line = None
                break
            line = self._pending_line
            self._pending_line = following
            point = _parse_point(line)
            self._hasher.update(line.rstrip("\n").encode("utf-8"))
            self._hasher.update(b"\n")
            self._point_count += 1
            if seen is not None:
                key = (point.scope, _point_id_key(point.point_id))
                if key in seen:
                    raise SnapshotIntegrityError(
                        f"duplicate point id in snapshot: scope={point.scope!r} "
                        f"point_id={point.point_id!r}"
                    )
                seen.add(key)
            yield point
        if self._trailer is None:
            raise SnapshotIntegrityError(f"snapshot is missing a trailer line: {self._path}")

    def close(self) -> None:
        self._text_stream.close()
        self._file.close()

    def __enter__(self) -> SnapshotReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _read_line(self) -> str | None:
        try:
            line = self._text_stream.readline()
        except zstandard.ZstdError as exc:
            raise SnapshotIntegrityError(
                f"corrupt zstd stream while reading {self._path}: {exc}"
            ) from exc
        return line if line else None

    def _finalize(self, trailer_line: str) -> None:
        trailer = _parse_trailer(trailer_line)
        computed_hash = self._hasher.hexdigest()
        if trailer.point_count != self._point_count:
            raise SnapshotIntegrityError(
                f"trailer point_count {trailer.point_count} does not match "
                f"{self._point_count} point line(s) actually read"
            )
        if trailer.content_hash != computed_hash:
            raise SnapshotIntegrityError(
                "trailer content_hash does not match the recomputed hash of point lines; "
                "the snapshot may have been tampered with or truncated"
            )
        self._trailer = trailer


# --------------------------------------------------------------------------
# NDJSON connector: read a snapshot file back as a `VectorTargetConnector`
# --------------------------------------------------------------------------


class NdjsonConnector(VectorTargetConnector[NormalizedPoint]):
    """Replays a committed `.ndjson.zst` snapshot as a read-only `VectorTargetConnector`.

    `normalize_point` is the identity function here: the raw record a
    snapshot file yields already *is* a `NormalizedPoint`. There is no
    live target to guard mutation against (this connector never opens
    a network or database connection at all), so section 42.2's
    transport-level guard does not apply to it; its read-only-ness is
    structural -- the file is opened for reading only, and nothing in
    this class ever writes back to it.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._path = Path(path)
        self._clock = clock
        self._consistency: ConsistencyInfo | None = None

    def validate_configuration(self) -> None:
        if not self._path.exists():
            raise ConnectorConfigError(f"snapshot file does not exist: {self._path}")

    def test_connection(self) -> ConnectionTestResult:
        try:
            with SnapshotReader(self._path) as reader:
                _ = reader.header
        except (SnapshotIntegrityError, OSError) as exc:
            return ConnectionTestResult(ok=False, message=str(exc))
        return ConnectionTestResult(ok=True, message="reachable")

    def inspect_target_schema(self) -> TargetSchema:
        try:
            with SnapshotReader(self._path) as reader:
                header = reader.header
        except (SnapshotIntegrityError, OSError) as exc:
            raise ConnectorConnectionError(f"failed to read snapshot header: {exc}") from exc
        vector_fields = tuple(
            VectorFieldSchema(name=name, dimension=header.vector_dimensions.get(name, 0))
            for name in header.vector_names
        )
        return TargetSchema(
            target_id=header.target_id,
            scope=header.scope,
            vector_fields=vector_fields,
            point_id_type="unknown",
            approx_point_count=None,
        )

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target_type="ndjson",
            supports_resume=False,
            supports_vector_hash=True,
            supports_sampling=True,
            max_page_size=None,
            consistency_modes=(
                ConsistencyMode.STRICT_CONSISTENT,
                ConsistencyMode.BEST_EFFORT_LIVE,
                ConsistencyMode.BEST_EFFORT_PAGED,
            ),
        )

    def estimate_count(self) -> int | None:
        return None

    def normalize_point(
        self, raw: NormalizedPoint, *, include_vectors: bool = False
    ) -> NormalizedPoint:
        return raw

    def iterate_points(
        self,
        *,
        checkpoint: Checkpoint | None = None,
        projection: Sequence[str] | None = None,
        include_vectors: bool = False,
    ) -> Iterator[NormalizedPoint]:
        """Replay every point, or resume just past ``checkpoint``.

        ``checkpoint`` is the canonical-JSON-string key
        (`_point_id_key`) of the last point a previous pass yielded.
        Because a `.ndjson.zst` snapshot is a compressed sequential
        stream with no index, resuming still re-reads and discards
        every line up to and including that point -- cheap for the
        small fixtures this connector is meant for, not a substitute
        for a live connector's native offset/keyset resume.
        """
        if checkpoint is not None and not isinstance(checkpoint, str):
            raise ConnectorConfigError("NdjsonConnector checkpoint must be a point-id key string")
        with SnapshotReader(self._path) as reader:
            skipping = checkpoint is not None
            observed_count = 0
            for point in reader.points():
                observed_count += 1
                if skipping:
                    if _point_id_key(point.point_id) == checkpoint:
                        skipping = False
                    continue
                yield apply_projection(point, projection)
            trailer = reader.trailer
        self._consistency = ConsistencyInfo(
            mode=ConsistencyMode.STRICT_CONSISTENT,
            completeness=SnapshotCompleteness(trailer.consistency_completeness),
            start_count=trailer.consistency_start_count,
            end_count=trailer.consistency_end_count,
            observed_count=observed_count,
            detail=None,
        )

    def get_consistency_info(self) -> ConsistencyInfo:
        if self._consistency is None:
            raise RuntimeError("iterate_points has not completed a pass yet")
        return self._consistency

    def close(self) -> None:
        return None
