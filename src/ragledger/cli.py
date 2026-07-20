"""Command line entry point for ragledger.

This module currently exposes only the commands that are implemented.
Pipeline, connector, and reconciliation subcommands are added in later
milestones as their underlying functionality lands.
"""

from __future__ import annotations

import typer

from ragledger import __version__

app = typer.Typer(
    name="ragledger",
    help="Lineage and integrity ledger for RAG knowledge bases.",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Lineage and integrity ledger for RAG knowledge bases.

    Registering an explicit callback keeps ragledger in subcommand mode
    (``ragledger <command>``) even while only one command is
    implemented; Typer would otherwise collapse a single-command app
    into a bare callable and drop the subcommand name.
    """


@app.command()
def version() -> None:
    """Print the installed ragledger version."""
    typer.echo(__version__)


def main() -> None:
    """Run the ragledger CLI."""
    app()


if __name__ == "__main__":
    main()
