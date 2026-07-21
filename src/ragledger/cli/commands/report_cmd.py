"""`ragledger report manifest|snapshot`, per PROJECT_SPEC.md section 17.1 and 23.

`--format json` writes the canonical (RFC 8785) JSON bytes of
`ragledger.reports.build_manifest_report`/`build_snapshot_report`;
`--format html` writes the matching self-contained HTML page. Output
goes to `--output` when given, stdout otherwise.
"""

from __future__ import annotations

from pathlib import Path

import jsonschema
import typer
from pydantic import ValidationError

from ragledger.cli._exit import EXIT_CONFIG_ERROR, CliError, run_command
from ragledger.cli._output import emit_text, log
from ragledger.connectors.ndjson import SnapshotIntegrityError
from ragledger.core.canonical import canonical_bytes
from ragledger.core.manifest import load_manifest
from ragledger.reports import (
    build_manifest_report,
    build_snapshot_report,
    render_manifest_report_html,
    render_snapshot_report_html,
)

app = typer.Typer(help="Generate manifest/snapshot reports.", no_args_is_help=True)

_FORMATS = ("json", "html")


def _write_report(output: Path | None, text: str) -> None:
    if output is None:
        emit_text(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    log(f"wrote {output}")


@app.command("manifest")
def manifest_report(
    manifest: Path = typer.Argument(..., help="Path to the manifest JSON file."),  # noqa: B008
    format: str = typer.Option("json", "--format", help="json or html."),
    output: Path | None = typer.Option(  # noqa: B008
        None, "--output", help="Where to write the report (default: stdout)."
    ),
) -> None:
    """Render a MANIFEST summary report."""
    run_command(lambda: _manifest_report_impl(manifest, format, output))


def _manifest_report_impl(manifest_path: Path, fmt: str, output: Path | None) -> None:
    if fmt not in _FORMATS:
        raise CliError(
            f"--format must be one of {_FORMATS}, got {fmt!r}", exit_code=EXIT_CONFIG_ERROR
        )
    try:
        envelope = load_manifest(manifest_path)
    except (OSError, ValueError, jsonschema.exceptions.ValidationError) as exc:
        raise CliError(
            f"cannot load manifest {manifest_path}: {exc}", exit_code=EXIT_CONFIG_ERROR
        ) from exc

    if fmt == "json":
        text = canonical_bytes(build_manifest_report(envelope)).decode("utf-8")
    else:
        text = render_manifest_report_html(envelope)
    _write_report(output, text)


@app.command("snapshot")
def snapshot_report(
    snapshot: Path = typer.Argument(..., help="Path to the .ndjson.zst snapshot file."),  # noqa: B008
    format: str = typer.Option("json", "--format", help="json or html."),
    output: Path | None = typer.Option(  # noqa: B008
        None, "--output", help="Where to write the report (default: stdout)."
    ),
) -> None:
    """Render a SNAPSHOT summary report."""
    run_command(lambda: _snapshot_report_impl(snapshot, format, output))


def _snapshot_report_impl(snapshot_path: Path, fmt: str, output: Path | None) -> None:
    if fmt not in _FORMATS:
        raise CliError(
            f"--format must be one of {_FORMATS}, got {fmt!r}", exit_code=EXIT_CONFIG_ERROR
        )
    if not snapshot_path.is_file():
        raise CliError(f"snapshot file not found: {snapshot_path}", exit_code=EXIT_CONFIG_ERROR)
    try:
        if fmt == "json":
            text = canonical_bytes(build_snapshot_report(snapshot_path)).decode("utf-8")
        else:
            text = render_snapshot_report_html(snapshot_path)
    except (OSError, ValidationError, SnapshotIntegrityError) as exc:
        raise CliError(
            f"cannot load snapshot {snapshot_path}: {exc}", exit_code=EXIT_CONFIG_ERROR
        ) from exc
    _write_report(output, text)
