"""Command-line entry point for ragledger, per the design specification section 17.1.

This package replaces the earlier single-module `ragledger.cli`;
`pyproject.toml`'s `[project.scripts]` entry (``ragledger =
"ragledger.cli:app"``) stays correct unchanged, since a package's
`__init__.py` is exactly where ``ragledger.cli:app`` resolves ``app``
from. Every subcommand is implemented in `ragledger.cli.commands.*` and
registered here; this module's only job is wiring, not command logic.

Commands implemented: `init`, `build`, `manifest validate/sign/verify`,
`key generate`, `target add`, `snapshot`, `report manifest/snapshot`,
`reconcile`, `version`. `inspect`, `diff`, `doctor`, and `serve` from
the design specification's full command list remain out of scope for
this release; they arrive with the server and web layers.
"""

from __future__ import annotations

import typer

from ragledger import __version__
from ragledger.cli.commands import (
    build_cmd,
    init_cmd,
    key_cmd,
    manifest_cmd,
    reconcile_cmd,
    report_cmd,
    snapshot_cmd,
    target_cmd,
)

app = typer.Typer(
    name="ragledger",
    help="Lineage and integrity ledger for RAG knowledge bases.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.callback()
def _root() -> None:
    """Lineage and integrity ledger for RAG knowledge bases.

    Registering an explicit callback keeps ragledger in subcommand mode
    (``ragledger <command>``) rather than Typer collapsing to a bare
    callable if only one command were ever registered.
    """


@app.command()
def version() -> None:
    """Print the installed ragledger version."""
    typer.echo(__version__)


app.command("init")(init_cmd.init)
app.command("build")(build_cmd.build)
app.add_typer(manifest_cmd.app, name="manifest")
app.add_typer(key_cmd.app, name="key")
app.add_typer(target_cmd.app, name="target")
app.command("snapshot")(snapshot_cmd.snapshot)
app.add_typer(report_cmd.app, name="report")
app.command("reconcile")(reconcile_cmd.reconcile)


def main() -> None:
    """Run the ragledger CLI."""
    app()


if __name__ == "__main__":
    main()
