"""Tests for `ragledger key generate`."""

from __future__ import annotations

import stat
from pathlib import Path

from typer.testing import CliRunner

from ragledger.cli import app


def test_generate_writes_a_usable_keypair(runner: CliRunner, tmp_path: Path) -> None:
    private_key = tmp_path / "signing.key"
    public_key = tmp_path / "signing.pub"
    result = runner.invoke(
        app,
        [
            "key",
            "generate",
            "--private-key-file",
            str(private_key),
            "--public-key-file",
            str(public_key),
        ],
    )
    assert result.exit_code == 0, result.output
    assert private_key.is_file()
    assert public_key.is_file()
    assert len(private_key.read_bytes()) == 32
    assert len(public_key.read_bytes()) == 32
    assert "key id" in result.output


def test_generate_private_key_file_mode_is_0600(runner: CliRunner, tmp_path: Path) -> None:
    private_key = tmp_path / "signing.key"
    runner.invoke(
        app,
        [
            "key",
            "generate",
            "--private-key-file",
            str(private_key),
            "--public-key-file",
            str(tmp_path / "signing.pub"),
        ],
    )
    mode = stat.S_IMODE(private_key.stat().st_mode)
    assert mode == 0o600


def test_generate_refuses_to_overwrite_without_force(runner: CliRunner, tmp_path: Path) -> None:
    private_key = tmp_path / "signing.key"
    public_key = tmp_path / "signing.pub"
    args = [
        "key",
        "generate",
        "--private-key-file",
        str(private_key),
        "--public-key-file",
        str(public_key),
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 0
    second = runner.invoke(app, args)
    assert second.exit_code == 1
    assert "already exists" in second.output


def test_generate_force_overwrites_and_rotates_the_key(runner: CliRunner, tmp_path: Path) -> None:
    private_key = tmp_path / "signing.key"
    public_key = tmp_path / "signing.pub"
    args = [
        "key",
        "generate",
        "--private-key-file",
        str(private_key),
        "--public-key-file",
        str(public_key),
    ]
    runner.invoke(app, args)
    first_public_bytes = public_key.read_bytes()

    result = runner.invoke(app, [*args, "--force"])
    assert result.exit_code == 0, result.output
    assert public_key.read_bytes() != first_public_bytes
