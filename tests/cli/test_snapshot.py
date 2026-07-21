"""Tests for `ragledger snapshot`, run against the committed NDJSON fixtures.

No live Qdrant/pgvector service: every test here uses `type: ndjson`
target configs pointing at `tests/fixtures/snapshots/*.ndjson.zst`,
exercised through `ragledger.connectors.ndjson.NdjsonConnector` exactly
as a real target connector would be, per that module's own documented
purpose.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ragledger.cli import app
from ragledger.connectors.ndjson import SnapshotReader
from ragledger.core.canonical import canonical_bytes


def _ndjson_target_config(tmp_path: Path, snapshot_path: Path) -> Path:
    config = tmp_path / "ndjson-target.yml"
    config.write_text(f"type: ndjson\npath: {snapshot_path.as_posix()}\n", encoding="utf-8")
    return config


def test_snapshot_full_pass_from_ndjson_fixture(
    runner: CliRunner, snapshots_dir: Path, tmp_path: Path
) -> None:
    target = _ndjson_target_config(tmp_path, snapshots_dir / "qdrant_support_kb.ndjson.zst")
    output = tmp_path / "out.ndjson.zst"
    result = runner.invoke(app, ["snapshot", str(target), "--output", str(output), "--epoch", "0"])
    assert result.exit_code == 0, result.output
    assert "points=3" in result.output
    assert output.is_file()

    with SnapshotReader(output) as reader:
        points = list(reader.points())
        assert len(points) == 3
        assert reader.trailer.point_count == 3


def test_snapshot_writes_a_resume_checkpoint_sidecar(
    runner: CliRunner, snapshots_dir: Path, tmp_path: Path
) -> None:
    target = _ndjson_target_config(tmp_path, snapshots_dir / "qdrant_support_kb.ndjson.zst")
    output = tmp_path / "out.ndjson.zst"
    result = runner.invoke(app, ["snapshot", str(target), "--output", str(output)])
    assert result.exit_code == 0, result.output
    sidecar = tmp_path / "out.ndjson.zst.checkpoint.json"
    assert sidecar.is_file()
    checkpoint = json.loads(sidecar.read_text(encoding="utf-8"))["checkpoint"]
    assert isinstance(checkpoint, str)


def test_snapshot_explicit_checkpoint_skips_forward(
    runner: CliRunner, snapshots_dir: Path, tmp_path: Path
) -> None:
    fixture = snapshots_dir / "qdrant_support_kb.ndjson.zst"
    target = _ndjson_target_config(tmp_path, fixture)

    with SnapshotReader(fixture) as reader:
        all_points = list(reader.points())
    first_point_id = all_points[0].point_id
    # NdjsonConnector's checkpoint is the canonical-JSON string of a point_id
    # (`ragledger.connectors.ndjson._point_id_key`); --checkpoint additionally
    # JSON-decodes its argument, so that string must itself be JSON-quoted.
    point_id_key = canonical_bytes(first_point_id).decode("utf-8")

    output = tmp_path / "resumed.ndjson.zst"
    result = runner.invoke(
        app,
        [
            "snapshot",
            str(target),
            "--output",
            str(output),
            "--checkpoint",
            json.dumps(point_id_key),
        ],
    )
    assert result.exit_code == 0, result.output
    with SnapshotReader(output) as reader:
        resumed_points = list(reader.points())
    assert len(resumed_points) == len(all_points) - 1


def test_snapshot_resume_flag_reuses_the_sidecar(
    runner: CliRunner, snapshots_dir: Path, tmp_path: Path
) -> None:
    target = _ndjson_target_config(tmp_path, snapshots_dir / "qdrant_support_kb.ndjson.zst")
    output = tmp_path / "out.ndjson.zst"

    first = runner.invoke(app, ["snapshot", str(target), "--output", str(output)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, ["snapshot", str(target), "--output", str(output), "--resume"])
    assert second.exit_code == 0, second.output
    assert "resuming from checkpoint sidecar" in second.output
    with SnapshotReader(output) as reader:
        assert list(reader.points()) == []  # already at the end of the fixture


def test_snapshot_malformed_checkpoint_json_is_a_config_error(
    runner: CliRunner, snapshots_dir: Path, tmp_path: Path
) -> None:
    target = _ndjson_target_config(tmp_path, snapshots_dir / "qdrant_support_kb.ndjson.zst")
    result = runner.invoke(
        app,
        [
            "snapshot",
            str(target),
            "--output",
            str(tmp_path / "out.ndjson.zst"),
            "--checkpoint",
            "not json",
        ],
    )
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_snapshot_checkpoint_of_the_wrong_shape_is_a_config_error(
    runner: CliRunner, snapshots_dir: Path, tmp_path: Path
) -> None:
    target = _ndjson_target_config(tmp_path, snapshots_dir / "qdrant_support_kb.ndjson.zst")
    result = runner.invoke(
        app,
        [
            "snapshot",
            str(target),
            "--output",
            str(tmp_path / "out.ndjson.zst"),
            # NdjsonConnector requires a string checkpoint; an integer is malformed.
            "--checkpoint",
            "1",
        ],
    )
    assert result.exit_code == 1
    assert "invalid --checkpoint" in result.output


def test_snapshot_missing_target_config_file_is_a_config_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        ["snapshot", str(tmp_path / "missing.yml"), "--output", str(tmp_path / "out.ndjson.zst")],
    )
    assert result.exit_code == 1
    assert "cannot read target config" in result.output


def test_snapshot_target_config_missing_type_is_a_config_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "target.yml"
    target.write_text("path: somewhere.ndjson.zst\n", encoding="utf-8")
    result = runner.invoke(
        app, ["snapshot", str(target), "--output", str(tmp_path / "out.ndjson.zst")]
    )
    assert result.exit_code == 1
    assert "unknown or missing 'type'" in result.output


def test_snapshot_source_file_does_not_exist_is_a_config_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    target = _ndjson_target_config(tmp_path, tmp_path / "does-not-exist.ndjson.zst")
    result = runner.invoke(
        app, ["snapshot", str(target), "--output", str(tmp_path / "out.ndjson.zst")]
    )
    assert result.exit_code == 1
    assert "target configuration invalid" in result.output


def test_snapshot_pgvector_fixture_round_trips_composite_point_ids(
    runner: CliRunner, snapshots_dir: Path, tmp_path: Path
) -> None:
    target = _ndjson_target_config(tmp_path, snapshots_dir / "pgvector_document_chunks.ndjson.zst")
    output = tmp_path / "out.ndjson.zst"
    result = runner.invoke(app, ["snapshot", str(target), "--output", str(output)])
    assert result.exit_code == 0, result.output
    with SnapshotReader(output) as reader:
        points = list(reader.points())
    assert len(points) == 3
    assert any(isinstance(point.point_id, dict) for point in points)
