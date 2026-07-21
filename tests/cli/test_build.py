"""Tests for `ragledger build`."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragledger.cli import app


def _build_args(
    root: Path, config: Path, output: Path, artifacts: Path, cache: Path, *extra: str
) -> list[str]:
    return [
        "build",
        str(root),
        "--config",
        str(config),
        "--output",
        str(output),
        "--artifacts",
        str(artifacts),
        "--cache",
        str(cache),
        *extra,
    ]


def test_build_produces_a_manifest_with_expected_statistics(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    config = write_minimal_config(tmp_path / "ragledger.yml", root=corpus_dir)
    output = tmp_path / "manifest.json"
    result = runner.invoke(
        app,
        _build_args(
            corpus_dir, config, output, tmp_path / "artifacts", tmp_path / "cache", "--epoch", "0"
        ),
    )
    assert result.exit_code == 0, result.output
    assert output.is_file()
    assert "status=complete" in result.output

    manifest = json.loads(output.read_bytes())
    assert manifest["statistics"]["source_count"] == 7
    assert manifest["statistics"]["chunk_count"] > 0
    assert manifest["statistics"]["embedding_count"] == manifest["statistics"]["chunk_count"]


def test_build_twice_with_the_same_epoch_is_byte_identical(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    config = write_minimal_config(tmp_path / "ragledger.yml", root=corpus_dir)
    output1 = tmp_path / "manifest1.json"
    output2 = tmp_path / "manifest2.json"

    result1 = runner.invoke(
        app,
        _build_args(
            corpus_dir,
            config,
            output1,
            tmp_path / "artifacts1",
            tmp_path / "cache1",
            "--epoch",
            "1700000000",
        ),
    )
    result2 = runner.invoke(
        app,
        _build_args(
            corpus_dir,
            config,
            output2,
            tmp_path / "artifacts2",
            tmp_path / "cache2",
            "--epoch",
            "1700000000",
        ),
    )
    assert result1.exit_code == 0, result1.output
    assert result2.exit_code == 0, result2.output
    assert output1.read_bytes() == output2.read_bytes()


def test_build_source_date_epoch_env_var_is_honored(
    runner: CliRunner,
    corpus_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_minimal_config: Callable[..., Path],
) -> None:
    config = write_minimal_config(tmp_path / "ragledger.yml", root=corpus_dir)
    output = tmp_path / "manifest.json"
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    result = runner.invoke(
        app, _build_args(corpus_dir, config, output, tmp_path / "artifacts", tmp_path / "cache")
    )
    assert result.exit_code == 0, result.output

    manifest = json.loads(output.read_bytes())
    assert manifest["created_at"] == "2023-11-14T22:13:20Z"


def test_build_missing_source_root_is_a_config_error(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    config = write_minimal_config(tmp_path / "ragledger.yml", root=corpus_dir)
    missing_root = tmp_path / "does-not-exist"
    result = runner.invoke(
        app,
        _build_args(
            missing_root,
            config,
            tmp_path / "manifest.json",
            tmp_path / "artifacts",
            tmp_path / "cache",
        ),
    )
    assert result.exit_code == 1
    assert "not a directory" in result.output


def test_build_malformed_yaml_config_is_a_config_error(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path
) -> None:
    config = tmp_path / "ragledger.yml"
    config.write_text("not: [valid", encoding="utf-8")
    result = runner.invoke(
        app,
        _build_args(
            corpus_dir,
            config,
            tmp_path / "manifest.json",
            tmp_path / "artifacts",
            tmp_path / "cache",
        ),
    )
    assert result.exit_code == 1
    assert "not valid YAML" in result.output


def test_build_unknown_config_key_is_a_config_error(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path
) -> None:
    config = tmp_path / "ragledger.yml"
    config.write_text(
        f"version: 1\nnamespace: x\nsources:\n  root: {corpus_dir.as_posix()}\nbogus_field: true\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        _build_args(
            corpus_dir,
            config,
            tmp_path / "manifest.json",
            tmp_path / "artifacts",
            tmp_path / "cache",
        ),
    )
    assert result.exit_code == 1
    assert "failed validation" in result.output


def test_build_unknown_chunker_strategy_is_a_config_error(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    config = write_minimal_config(
        tmp_path / "ragledger.yml",
        root=corpus_dir,
        extra="chunker:\n  strategy: not-a-real-chunker\n",
    )
    result = runner.invoke(
        app,
        _build_args(
            corpus_dir,
            config,
            tmp_path / "manifest.json",
            tmp_path / "artifacts",
            tmp_path / "cache",
        ),
    )
    assert result.exit_code == 1
    assert "chunker.strategy" in result.output


def test_build_incomplete_status_exits_with_policy_fail_code(
    runner: CliRunner, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "unsupported.bin").write_bytes(bytes(range(256)))  # no registered parser
    config = write_minimal_config(tmp_path / "ragledger.yml", root=root)
    output = tmp_path / "manifest.json"
    result = runner.invoke(
        app, _build_args(root, config, output, tmp_path / "artifacts", tmp_path / "cache")
    )
    assert result.exit_code == 3
    assert output.is_file()  # the partial manifest is still written
    assert json.loads(output.read_bytes())["build"]["status"] == "incomplete"


def test_build_allow_incomplete_flag_exits_zero(
    runner: CliRunner, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "unsupported.bin").write_bytes(bytes(range(256)))
    config = write_minimal_config(tmp_path / "ragledger.yml", root=root)
    result = runner.invoke(
        app,
        _build_args(
            root,
            config,
            tmp_path / "manifest.json",
            tmp_path / "artifacts",
            tmp_path / "cache",
            "--allow-incomplete",
        ),
    )
    assert result.exit_code == 0, result.output


def test_build_embedding_mode_none_produces_no_embeddings(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    config = write_minimal_config(
        tmp_path / "ragledger.yml", root=corpus_dir, extra="embedding:\n  mode: none\n"
    )
    output = tmp_path / "manifest.json"
    result = runner.invoke(
        app, _build_args(corpus_dir, config, output, tmp_path / "artifacts", tmp_path / "cache")
    )
    assert result.exit_code == 0, result.output

    manifest = json.loads(output.read_bytes())
    assert manifest["statistics"]["embedding_count"] == 0
    assert manifest["statistics"]["chunk_count"] > 0


def test_build_local_mode_without_lock_file_is_rejected(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    config = write_minimal_config(
        tmp_path / "ragledger.yml",
        root=corpus_dir,
        extra="embedding:\n  mode: local\n  revision_file: ./model-revisions.lock\n",
    )
    result = runner.invoke(
        app,
        _build_args(
            corpus_dir,
            config,
            tmp_path / "manifest.json",
            tmp_path / "artifacts",
            tmp_path / "cache",
        ),
    )
    assert result.exit_code == 1
    assert "model-revisions.lock" in result.output


def test_build_local_mode_with_mutable_alias_is_rejected(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    lock_path = tmp_path / "model-revisions.lock"
    lock_path.write_text(
        "models:\n  sentence-transformers/all-MiniLM-L6-v2:\n    revision: main\n",
        encoding="utf-8",
    )
    config = write_minimal_config(
        tmp_path / "ragledger.yml",
        root=corpus_dir,
        extra="embedding:\n  mode: local\n  revision_file: ./model-revisions.lock\n",
    )
    result = runner.invoke(
        app,
        _build_args(
            corpus_dir,
            config,
            tmp_path / "manifest.json",
            tmp_path / "artifacts",
            tmp_path / "cache",
        ),
    )
    assert result.exit_code == 1
    assert "mutable alias" in result.output


def test_build_local_mode_with_pinned_revision_succeeds(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    lock_path = tmp_path / "model-revisions.lock"
    lock_path.write_text(
        "models:\n"
        "  sentence-transformers/all-MiniLM-L6-v2:\n"
        "    revision: 8b3219a92973c328a8e22fadcfa821b5dc75636\n",
        encoding="utf-8",
    )
    config = write_minimal_config(
        tmp_path / "ragledger.yml",
        root=corpus_dir,
        extra="embedding:\n  mode: local\n  revision_file: ./model-revisions.lock\n",
    )
    output = tmp_path / "manifest.json"
    result = runner.invoke(
        app, _build_args(corpus_dir, config, output, tmp_path / "artifacts", tmp_path / "cache")
    )
    assert result.exit_code == 0, result.output
    assert "this release's local embedding backend is the deterministic" in result.output

    manifest = json.loads(output.read_bytes())
    assert manifest["statistics"]["embedding_count"] == manifest["statistics"]["chunk_count"]
