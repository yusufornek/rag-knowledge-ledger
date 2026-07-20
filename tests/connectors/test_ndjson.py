"""Tests for `ragledger.connectors.ndjson`: writer/reader, integrity checks, `NdjsonConnector`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import zstandard

from ragledger.connectors.base import (
    ConsistencyInfo,
    ConsistencyMode,
    NormalizedPoint,
    SnapshotCompleteness,
    compute_payload_hash,
    hash_vector,
)
from ragledger.connectors.ndjson import (
    NdjsonConnector,
    SnapshotHeader,
    SnapshotIntegrityError,
    SnapshotReader,
    write_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "snapshots"

STARTED_AT = datetime(2026, 1, 1, tzinfo=UTC)
FINISHED_AT = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC)


def _header(**overrides: object) -> SnapshotHeader:
    fields: dict[str, object] = {
        "target_id": "support_kb",
        "scope": "support_kb",
        "target_type": "qdrant",
        "vector_names": ["dense"],
        "vector_dimensions": {"dense": 3},
        "started_at": STARTED_AT,
        "connector_version": "1",
        "consistency_mode": ConsistencyMode.BEST_EFFORT_LIVE.value,
    }
    fields.update(overrides)
    return SnapshotHeader.model_validate(fields)


def _point(index: int, *, scope: str = "support_kb") -> NormalizedPoint:
    projection = {"source_id": f"src_{index}", "chunk_id": f"chk_{index}", "tenant": "acme"}
    return NormalizedPoint(
        target_id=scope,
        scope=scope,
        point_id=f"pt-{index}",
        vector_names=["dense"],
        vector_dimensions={"dense": 3},
        vector_hashes={"dense": hash_vector([0.1, 0.2, 0.3])},
        payload_projection=projection,
        payload_hash=compute_payload_hash(projection),
        source_id=projection["source_id"],
        chunk_id=projection["chunk_id"],
        tenant=projection["tenant"],
        observed_at=OBSERVED_AT,
        raw_locator=f"qdrant:{scope}#pt-{index}",
    )


def _consistency() -> ConsistencyInfo:
    return ConsistencyInfo(
        mode=ConsistencyMode.BEST_EFFORT_LIVE,
        completeness=SnapshotCompleteness.COMPLETE,
        start_count=2,
        end_count=2,
        observed_count=2,
    )


# --------------------------------------------------------------------------
# Roundtrip
# --------------------------------------------------------------------------


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    points = [_point(1), _point(2)]
    trailer = write_snapshot(
        path, _header(), points, finished_at=FINISHED_AT, consistency_provider=_consistency
    )

    assert trailer.point_count == 2

    with SnapshotReader(path) as reader:
        assert reader.header.target_id == "support_kb"
        assert reader.header.consistency_mode == ConsistencyMode.BEST_EFFORT_LIVE.value
        read_points = list(reader.points())
        assert read_points == points
        assert reader.trailer.point_count == 2
        assert reader.trailer.content_hash == trailer.content_hash
        assert reader.trailer.consistency_completeness == SnapshotCompleteness.COMPLETE.value
        assert reader.trailer.consistency_start_count == 2


def test_write_and_read_empty_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "empty.ndjson.zst"
    write_snapshot(path, _header(), [], finished_at=FINISHED_AT)

    with SnapshotReader(path) as reader:
        points = list(reader.points())
        assert points == []
        assert reader.trailer.point_count == 0


def test_write_snapshot_without_consistency_provider_defaults_to_complete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snap.ndjson.zst"
    trailer = write_snapshot(path, _header(), [_point(1)], finished_at=FINISHED_AT)
    assert trailer.consistency_completeness == SnapshotCompleteness.COMPLETE.value
    assert trailer.consistency_start_count is None


def test_trailer_is_only_available_after_points_fully_consumed(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    write_snapshot(path, _header(), [_point(1), _point(2)], finished_at=FINISHED_AT)

    with SnapshotReader(path) as reader:
        with pytest.raises(RuntimeError):
            _ = reader.trailer
        list(reader.points())
        assert reader.trailer.point_count == 2


def test_points_can_only_be_consumed_once(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    write_snapshot(path, _header(), [_point(1)], finished_at=FINISHED_AT)

    with SnapshotReader(path) as reader:
        list(reader.points())
        with pytest.raises(RuntimeError):
            list(reader.points())


# --------------------------------------------------------------------------
# Integrity: tamper detection
# --------------------------------------------------------------------------


def _tamper(path: Path, transform: Callable[[bytes], bytes]) -> None:
    """Decompress ``path``, apply ``transform`` to the plaintext, and recompress."""
    decompressor = zstandard.ZstdDecompressor()
    with path.open("rb") as handle:
        plaintext = decompressor.stream_reader(handle).read()
    tampered = transform(plaintext)
    compressor = zstandard.ZstdCompressor(write_checksum=True)
    with path.open("wb") as handle, compressor.stream_writer(handle) as writer:
        writer.write(tampered)


def test_tampered_point_line_fails_content_hash_verification(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    write_snapshot(path, _header(), [_point(1), _point(2)], finished_at=FINISHED_AT)

    def corrupt_point(raw: bytes) -> bytes:
        lines = raw.decode("utf-8").splitlines()
        lines[1] = lines[1].replace('"pt-1"', '"pt-9999"')
        return ("\n".join(lines) + "\n").encode("utf-8")

    _tamper(path, corrupt_point)

    with (
        pytest.raises(SnapshotIntegrityError, match="content_hash"),
        SnapshotReader(path) as reader,
    ):
        list(reader.points())


def test_tampered_trailer_count_fails_verification(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    write_snapshot(path, _header(), [_point(1), _point(2)], finished_at=FINISHED_AT)

    def corrupt_trailer_count(raw: bytes) -> bytes:
        lines = raw.decode("utf-8").splitlines()
        lines[-1] = lines[-1].replace('"point_count":2', '"point_count":99')
        return ("\n".join(lines) + "\n").encode("utf-8")

    _tamper(path, corrupt_trailer_count)

    with pytest.raises(SnapshotIntegrityError, match="point_count"), SnapshotReader(path) as reader:
        list(reader.points())


def test_missing_trailer_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    write_snapshot(path, _header(), [_point(1)], finished_at=FINISHED_AT)

    def drop_trailer(raw: bytes) -> bytes:
        lines = raw.decode("utf-8").splitlines()
        return ("\n".join(lines[:-1]) + "\n").encode("utf-8")

    _tamper(path, drop_trailer)

    with pytest.raises(SnapshotIntegrityError, match="trailer"), SnapshotReader(path) as reader:
        list(reader.points())


def test_empty_file_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "empty.ndjson.zst"
    compressor = zstandard.ZstdCompressor()
    with path.open("wb") as handle, compressor.stream_writer(handle):
        pass

    with pytest.raises(SnapshotIntegrityError, match="header"):
        SnapshotReader(path)


def test_corrupted_zstd_bytes_are_detected(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    write_snapshot(path, _header(), [_point(i) for i in range(1, 6)], finished_at=FINISHED_AT)

    data = bytearray(path.read_bytes())
    mid = len(data) // 2
    data[mid] ^= 0xFF
    path.write_bytes(bytes(data))

    with pytest.raises(SnapshotIntegrityError), SnapshotReader(path) as reader:
        list(reader.points())


def test_duplicate_point_id_detected_when_enabled(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    write_snapshot(path, _header(), [_point(1), _point(1)], finished_at=FINISHED_AT)

    with pytest.raises(SnapshotIntegrityError, match="duplicate"), SnapshotReader(path) as reader:
        list(reader.points(check_duplicates=True))


def test_duplicate_point_id_ignored_by_default(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    write_snapshot(path, _header(), [_point(1), _point(1)], finished_at=FINISHED_AT)

    with SnapshotReader(path) as reader:
        points = list(reader.points())
    assert len(points) == 2


# --------------------------------------------------------------------------
# Large stream: bounded-memory-style API check
# --------------------------------------------------------------------------


def test_write_and_read_100k_points_streaming(tmp_path: Path) -> None:
    path = tmp_path / "large.ndjson.zst"
    total = 100_000

    def generate_points() -> Iterator[NormalizedPoint]:
        # Built lazily, one at a time: this generator is never
        # materialized into a list, matching how a live connector's
        # `iterate_points` would feed `write_snapshot`.
        for i in range(total):
            yield _point(i)

    trailer = write_snapshot(path, _header(), generate_points(), finished_at=FINISHED_AT)
    assert trailer.point_count == total

    seen = 0
    first_point_id: str | None = None
    last_point_id: str | None = None
    with SnapshotReader(path) as reader:
        for point in reader.points():
            if seen == 0:
                first_point_id = str(point.point_id)
            last_point_id = str(point.point_id)
            seen += 1
        assert reader.trailer.point_count == total

    assert seen == total
    assert first_point_id == "pt-0"
    assert last_point_id == f"pt-{total - 1}"


# --------------------------------------------------------------------------
# Committed fixtures
# --------------------------------------------------------------------------


def test_qdrant_fixture_is_valid_and_stable() -> None:
    path = FIXTURES_DIR / "qdrant_support_kb.ndjson.zst"
    with SnapshotReader(path) as reader:
        assert reader.header.target_type == "qdrant"
        points = list(reader.points())
        assert [point.point_id for point in points] == [1, 2, 3]
        assert reader.trailer.point_count == 3


def test_pgvector_fixture_has_composite_point_ids() -> None:
    path = FIXTURES_DIR / "pgvector_document_chunks.ndjson.zst"
    with SnapshotReader(path) as reader:
        assert reader.header.target_type == "pgvector"
        points = list(reader.points())
        assert points[0].point_id == {"tenant_id": "acme", "chunk_id": "chk_1"}
        assert reader.trailer.point_count == 3


# --------------------------------------------------------------------------
# NdjsonConnector
# --------------------------------------------------------------------------


def test_ndjson_connector_implements_vector_target_connector(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    points = [_point(1), _point(2), _point(3)]
    write_snapshot(
        path, _header(), points, finished_at=FINISHED_AT, consistency_provider=_consistency
    )

    connector = NdjsonConnector(path)
    connector.validate_configuration()
    assert connector.test_connection().ok is True

    schema = connector.inspect_target_schema()
    assert schema.vector_fields[0].dimension == 3

    replayed = list(connector.iterate_points())
    assert [point.point_id for point in replayed] == ["pt-1", "pt-2", "pt-3"]

    consistency = connector.get_consistency_info()
    assert consistency.completeness is SnapshotCompleteness.COMPLETE
    connector.close()


def test_ndjson_connector_resumes_from_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "snap.ndjson.zst"
    points = [_point(1), _point(2), _point(3)]
    write_snapshot(path, _header(), points, finished_at=FINISHED_AT)

    connector = NdjsonConnector(path)
    resumed = list(connector.iterate_points(checkpoint='"pt-1"'))
    assert [point.point_id for point in resumed] == ["pt-2", "pt-3"]
    connector.close()


def test_ndjson_connector_validate_configuration_missing_file(tmp_path: Path) -> None:
    connector = NdjsonConnector(tmp_path / "does-not-exist.ndjson.zst")
    with pytest.raises(Exception, match="does not exist"):
        connector.validate_configuration()
