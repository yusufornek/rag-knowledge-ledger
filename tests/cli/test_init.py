"""Tests for `ragledger init`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ragledger.cli import app


def test_init_creates_config_and_ignore_file(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--namespace", "acme-support"])
    assert result.exit_code == 0, result.output
    config_path = tmp_path / "ragledger.yml"
    ignore_path = tmp_path / ".ragledgerignore"
    assert config_path.is_file()
    assert ignore_path.is_file()
    assert 'namespace: "acme-support"' in config_path.read_text(encoding="utf-8")


def test_init_generated_config_is_immediately_valid(runner: CliRunner, tmp_path: Path) -> None:
    """`ragledger init && ragledger build` must not require any extra setup."""
    from ragledger.cli._config import load_config

    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    config = load_config(tmp_path / "ragledger.yml")
    assert config.embedding.mode == "deterministic"


def test_init_refuses_to_overwrite_without_force(runner: CliRunner, tmp_path: Path) -> None:
    first = runner.invoke(app, ["init", str(tmp_path)])
    assert first.exit_code == 0

    second = runner.invoke(app, ["init", str(tmp_path)])
    assert second.exit_code == 1
    assert "already exists" in second.output
    assert "--force" in second.output


def test_init_force_overwrites(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path), "--namespace", "first"])
    result = runner.invoke(app, ["init", str(tmp_path), "--namespace", "second", "--force"])
    assert result.exit_code == 0, result.output
    assert 'namespace: "second"' in (tmp_path / "ragledger.yml").read_text(encoding="utf-8")


def test_init_namespace_with_special_characters_produces_valid_yaml(
    runner: CliRunner, tmp_path: Path
) -> None:
    import yaml

    result = runner.invoke(app, ["init", str(tmp_path), "--namespace", 'weird: "name\nvalue'])
    assert result.exit_code == 0, result.output
    data = yaml.safe_load((tmp_path / "ragledger.yml").read_text(encoding="utf-8"))
    assert data["namespace"] == 'weird: "name\nvalue'
