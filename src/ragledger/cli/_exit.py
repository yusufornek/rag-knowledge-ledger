"""CLI exit codes, per the design specification section 17.1's exit code table.

A command body signals failure by raising `CliError` with the exit code
the process should terminate with, rather than calling `sys.exit`/
`raise typer.Exit` directly at the point of failure. `run_command`
(the single place every command's Typer-facing wrapper delegates to)
is the only place that ever converts an exception into a process exit
code, which keeps the exit-code table in one place instead of scattered
magic integers through command bodies, and guarantees an unexpected,
un-anticipated exception still exits `6` ("Internal error") rather than
crashing with a raw Python traceback or, worse, exit code `1`.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

EXIT_SUCCESS = 0
"""Success / policy pass."""

EXIT_CONFIG_ERROR = 1
"""Config/input/schema error."""

EXIT_FINDINGS = 2
"""Findings are present, but this is not a hard gate failure."""

EXIT_POLICY_FAIL = 3
"""Policy fail (default-fail behavior, e.g. an incomplete build)."""

EXIT_TARGET_FAILURE = 4
"""Target/build external failure (network, auth, unreachable target)."""

EXIT_INTEGRITY_FAILURE = 5
"""Signature/integrity failure."""

EXIT_INTERNAL_ERROR = 6
"""Internal error: an exception this command did not anticipate."""

EXIT_CANCELLED = 130
"""Cancelled (SIGINT)."""


class CliError(Exception):
    """Raised by a command body to terminate the process with a specific exit code.

    ``message`` is written to stderr, never stdout -- the design specification
    section 17.1: "JSON output stdout, logs stderr". Never construct
    this with a secret value interpolated into ``message``; nothing in
    this CLI ever echoes a resolved credential, private key, or DSN.
    """

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def run_command(body: Callable[[], None]) -> None:
    """Invoke a command's implementation, translating exceptions into exit codes.

    Every Typer-facing command function in `ragledger.cli.commands.*` is
    a thin wrapper that calls ``run_command(lambda: _impl(...))`` rather
    than putting logic directly in the Typer-decorated function: this
    keeps the actual implementation a plain, easily testable function
    while `run_command` is the single, shared boundary that maps
    `CliError` to its declared exit code and any other exception to
    `EXIT_INTERNAL_ERROR` -- never a silent success, never a bare stack
    trace on stdout.
    """
    try:
        body()
    except CliError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=exc.exit_code) from None
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        typer.echo("cancelled", err=True)
        raise typer.Exit(code=EXIT_CANCELLED) from None
    except Exception as exc:  # noqa: BLE001 - last-resort internal-error boundary
        typer.echo(f"internal error: {exc}", err=True)
        raise typer.Exit(code=EXIT_INTERNAL_ERROR) from None
