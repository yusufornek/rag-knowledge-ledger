"""`ragledger key generate`: create a new Ed25519 manifest-signing keypair.

The design specification's CLI command list does not enumerate a
standalone key-generation command, only `manifest sign`/`manifest
verify` consuming already-existing key files. `key generate` exists to
make those two commands usable end to end without an out-of-band
key-generation step.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ragledger.cli._exit import EXIT_CONFIG_ERROR, CliError, run_command
from ragledger.cli._output import log
from ragledger.core.signing import (
    fingerprint,
    generate_keypair,
    write_private_key,
    write_public_key,
)

app = typer.Typer(help="Manage Ed25519 manifest-signing keys.", no_args_is_help=True)


@app.command("generate")
def generate(
    private_key_file: Path = typer.Option(  # noqa: B008
        Path("signing.key"),
        "--private-key-file",
        help="Where to write the raw Ed25519 private key (file mode 0600).",
    ),
    public_key_file: Path = typer.Option(  # noqa: B008
        Path("signing.pub"),
        "--public-key-file",
        help="Where to write the raw Ed25519 public key.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing key files."),
) -> None:
    """Generate a new Ed25519 keypair for `ragledger manifest sign`/`verify`."""
    run_command(lambda: _generate_impl(private_key_file, public_key_file, force))


def _generate_impl(private_key_file: Path, public_key_file: Path, force: bool) -> None:
    for existing in (private_key_file, public_key_file):
        if existing.exists() and not force:
            raise CliError(
                f"{existing} already exists; pass --force to overwrite",
                exit_code=EXIT_CONFIG_ERROR,
            )

    private_key, public_key = generate_keypair()
    private_key_file.parent.mkdir(parents=True, exist_ok=True)
    public_key_file.parent.mkdir(parents=True, exist_ok=True)
    write_private_key(private_key, private_key_file)
    write_public_key(public_key, public_key_file)
    log(f"wrote {private_key_file} (mode 0600) and {public_key_file}")
    log(f"key id (sha256 fingerprint): {fingerprint(public_key)}")
