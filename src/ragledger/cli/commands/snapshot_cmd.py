"""`ragledger snapshot`, per PROJECT_SPEC.md section 17.1 and 13.5.

Streams a target's points to a `.ndjson.zst` snapshot file
(`ragledger.connectors.ndjson.write_snapshot`), then reports the pass's
consistency outcome (`ConsistencyInfo`).

Checkpoint/resume (FR-092's "resumable checkpoint"), scoped honestly for
a single synchronous CLI invocation: `--checkpoint` passes an explicit,
opaque JSON-encoded resume token straight to `iterate_points`, letting
an operator continue reading the *source* connector from a known point.
After every run that observed at least one point, this command writes a
`<output>.checkpoint.json` sidecar recording the last point's checkpoint
value; `--resume` reads that sidecar instead of requiring `--checkpoint`
to be typed out by hand. This is verified correct against
`NdjsonConnector` (its checkpoint format -- a canonical-JSON string key
of `point_id` -- is exactly what `ragledger.connectors.ndjson` documents
and this milestone's tests exercise). For Qdrant/pgvector the sidecar
stores the observed `point_id` value directly as a best-effort
checkpoint; see `docs/reviews/m4-status-notes.md` for why that is not
verified against a live target in this milestone. A resumed run
overwrites `--output` with only the newly read tail, not a merge with
the prior file -- operators who need one continuous file across resumed
runs should use distinct `--output` paths per chunk.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import typer

from ragledger.cli._build_support import resolve_epoch
from ragledger.cli._config import ConfigError
from ragledger.cli._exit import EXIT_CONFIG_ERROR, EXIT_TARGET_FAILURE, CliError, run_command
from ragledger.cli._output import log
from ragledger.cli._target import (
    TargetConfigError,
    build_connector,
    load_target_config,
    predicted_consistency_mode,
)
from ragledger.connectors.base import (
    ConnectorConfigError,
    ConnectorConnectionError,
    NormalizedPoint,
    SnapshotCompleteness,
)
from ragledger.connectors.ndjson import SnapshotHeader, write_snapshot
from ragledger.core.canonical import JSONValue, canonical_bytes
from ragledger.core.models import PointId

_CONNECTOR_VERSION = "1"


def snapshot(
    target: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the target config YAML file."
    ),
    output: Path = typer.Option(  # noqa: B008
        ..., "--output", help="Where to write the .ndjson.zst snapshot."
    ),
    checkpoint: str | None = typer.Option(
        None,
        "--checkpoint",
        help="A JSON-encoded resume token; skips the source connector forward past it.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Reuse the checkpoint sidecar next to --output from a previous run, if present.",
    ),
    include_vectors: bool = typer.Option(
        False, "--include-vectors", help="Fetch and hash raw vectors (never their raw floats)."
    ),
    epoch: int | None = typer.Option(
        None,
        "--epoch",
        help="Unix timestamp for started_at; falls back to SOURCE_DATE_EPOCH, then current time.",
    ),
) -> None:
    """Stream a target's points to an NDJSON snapshot, with checkpoint/resume and consistency."""
    run_command(lambda: _snapshot_impl(target, output, checkpoint, resume, include_vectors, epoch))


def _checkpoint_sidecar(output: Path) -> Path:
    return output.with_name(output.name + ".checkpoint.json")


def _checkpoint_value_for_resume(target_type: str, point_id: PointId) -> JSONValue:
    if target_type == "ndjson":
        # Mirrors `ragledger.connectors.ndjson._point_id_key`: NdjsonConnector
        # requires its checkpoint to be exactly this canonical-JSON string.
        return canonical_bytes(point_id).decode("utf-8")
    return point_id


def _resolve_checkpoint(checkpoint_arg: str | None, resume: bool, sidecar: Path) -> JSONValue:
    if checkpoint_arg is not None:
        try:
            parsed: JSONValue = json.loads(checkpoint_arg)
            return parsed
        except json.JSONDecodeError as exc:
            raise CliError(
                f"--checkpoint is not valid JSON: {exc}", exit_code=EXIT_CONFIG_ERROR
            ) from exc
    if resume and sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            checkpoint_value: JSONValue = data["checkpoint"]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise CliError(
                f"cannot read checkpoint sidecar {sidecar}: {exc}", exit_code=EXIT_CONFIG_ERROR
            ) from exc
        log(f"resuming from checkpoint sidecar {sidecar}")
        return checkpoint_value
    return None


def _snapshot_impl(
    target_path: Path,
    output: Path,
    checkpoint_arg: str | None,
    resume: bool,
    include_vectors: bool,
    epoch: int | None,
) -> None:
    try:
        target_config = load_target_config(target_path)
    except TargetConfigError as exc:
        raise CliError(str(exc), exit_code=EXIT_CONFIG_ERROR) from exc

    try:
        resolved_epoch = resolve_epoch(epoch)
    except ConfigError as exc:
        raise CliError(str(exc), exit_code=EXIT_CONFIG_ERROR) from exc
    started_at = (
        datetime.fromtimestamp(resolved_epoch, tz=UTC)
        if resolved_epoch is not None
        else datetime.now(UTC)
    )

    sidecar = _checkpoint_sidecar(output)
    checkpoint_value = _resolve_checkpoint(checkpoint_arg, resume, sidecar)

    connector = build_connector(target_config)
    try:
        try:
            connector.validate_configuration()
        except ConnectorConfigError as exc:
            raise CliError(
                f"target configuration invalid: {exc}", exit_code=EXIT_CONFIG_ERROR
            ) from exc

        test_result = connector.test_connection()
        if not test_result.ok:
            raise CliError(
                f"target unreachable: {test_result.message}", exit_code=EXIT_TARGET_FAILURE
            )

        schema = connector.inspect_target_schema()
        header = SnapshotHeader(
            target_id=schema.target_id,
            scope=schema.scope,
            target_type=target_config.type,
            vector_names=[field.name for field in schema.vector_fields],
            vector_dimensions={field.name: field.dimension for field in schema.vector_fields},
            started_at=started_at,
            connector_version=_CONNECTOR_VERSION,
            consistency_mode=predicted_consistency_mode(target_config),
        )

        output.parent.mkdir(parents=True, exist_ok=True)

        last_point_id: PointId | None = None
        points_seen = 0

        def _tracked_points() -> Iterator[NormalizedPoint]:
            nonlocal last_point_id, points_seen
            for point in connector.iterate_points(
                checkpoint=checkpoint_value, include_vectors=include_vectors
            ):
                last_point_id = point.point_id
                points_seen += 1
                yield point

        try:
            trailer = write_snapshot(
                output,
                header,
                _tracked_points(),
                finished_at=datetime.now(UTC),
                consistency_provider=connector.get_consistency_info,
            )
        except ConnectorConnectionError as exc:
            raise CliError(f"snapshot failed: {exc}", exit_code=EXIT_TARGET_FAILURE) from exc
        except ConnectorConfigError as exc:
            raise CliError(f"invalid --checkpoint: {exc}", exit_code=EXIT_CONFIG_ERROR) from exc

        consistency = connector.get_consistency_info()
    finally:
        connector.close()

    if consistency.completeness == SnapshotCompleteness.INCOMPLETE:
        log(
            f"WARNING: snapshot pass reported INCOMPLETE consistency "
            f"(start_count={consistency.start_count} end_count={consistency.end_count}); "
            "the target changed while this snapshot was being read"
        )

    if points_seen and last_point_id is not None:
        checkpoint_for_sidecar = _checkpoint_value_for_resume(target_config.type, last_point_id)
        sidecar.write_text(json.dumps({"checkpoint": checkpoint_for_sidecar}), encoding="utf-8")
        log(f"wrote resume checkpoint to {sidecar}")

    log(
        f"wrote {output}: points={trailer.point_count} "
        f"consistency={consistency.completeness.value} mode={consistency.mode.value} "
        f"content_hash={trailer.content_hash}"
    )
