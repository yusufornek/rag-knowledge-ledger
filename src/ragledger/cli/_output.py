"""Shared stdout/stderr conventions for `ragledger.cli` commands.

Per the design specification section 17.1: "JSON output stdout, logs stderr."
`log` is for human-readable status/progress lines (stderr); a command's
final structured result is printed with `emit_text` (stdout). Nothing in
this module ever receives or echoes a secret -- callers are responsible
for redacting before they call in, per this release's "secrets never
echoed" rule; there is no formatting convention here that could
accidentally surface one.
"""

from __future__ import annotations

import typer


def log(message: str) -> None:
    """Write one human-readable status/progress line to stderr."""
    typer.echo(message, err=True)


def emit_text(message: str) -> None:
    """Write one line of human-readable *result* text to stdout.

    Used for a command's already-serialized final output (for example
    `report_cmd`'s canonical JSON or self-contained HTML text), so every
    command shares one place that decides stdout is for results and
    stderr is for progress, per section 17.1.
    """
    typer.echo(message)
