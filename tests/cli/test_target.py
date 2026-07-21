"""Tests for `ragledger target add`.

No live Qdrant/pgvector service is available or permitted in this test
suite. `--no-check` exercises config validation without any network
call; the one `--check` test points at a loopback port nothing is
listening on, so the connector's own retry/backoff loop fails fast with
`ECONNREFUSED` rather than actually reaching a network.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ragledger.cli import app

_QDRANT_CONFIG = """\
type: qdrant
endpoint: http://127.0.0.1:1
collection: support_kb
connect_timeout_seconds: 0.2
read_timeout_seconds: 0.2
max_retries: 0
"""

_PGVECTOR_CONFIG = """\
type: pgvector
dsn_env: RAGLEDGER_TEST_PGVECTOR_DSN
table: document_chunks
primary_key: [id]
vector_column: embedding
"""


def test_add_qdrant_no_check_validates_config_shape_only(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "qdrant-target.yml"
    config.write_text(_QDRANT_CONFIG, encoding="utf-8")
    result = runner.invoke(app, ["target", "add", "qdrant", "--config", str(config), "--no-check"])
    assert result.exit_code == 0, result.output
    assert "valid qdrant target configuration" in result.output


def test_add_pgvector_no_check_validates_config_shape_only(
    runner: CliRunner, tmp_path: Path
) -> None:
    config = tmp_path / "pgvector-target.yml"
    config.write_text(_PGVECTOR_CONFIG, encoding="utf-8")
    result = runner.invoke(
        app, ["target", "add", "pgvector", "--config", str(config), "--no-check"]
    )
    assert result.exit_code == 0, result.output
    assert "valid pgvector target configuration" in result.output


def test_add_unsupported_target_type_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "qdrant-target.yml"
    config.write_text(_QDRANT_CONFIG, encoding="utf-8")
    result = runner.invoke(app, ["target", "add", "weaviate", "--config", str(config)])
    assert result.exit_code == 1
    assert "unsupported target type" in result.output


def test_add_config_type_mismatch_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "qdrant-target.yml"
    config.write_text(_QDRANT_CONFIG, encoding="utf-8")
    result = runner.invoke(
        app, ["target", "add", "pgvector", "--config", str(config), "--no-check"]
    )
    assert result.exit_code == 1
    assert "declares type" in result.output


def test_add_malformed_yaml_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "bad.yml"
    config.write_text("type: [qdrant", encoding="utf-8")
    result = runner.invoke(app, ["target", "add", "qdrant", "--config", str(config)])
    assert result.exit_code == 1
    assert "not valid YAML" in result.output


def test_add_missing_config_file_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["target", "add", "qdrant", "--config", str(tmp_path / "missing.yml")]
    )
    assert result.exit_code == 1
    assert "cannot read target config" in result.output


def test_add_invalid_field_value_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "qdrant-target.yml"
    config.write_text("type: qdrant\nendpoint: not-a-url\ncollection: x\n", encoding="utf-8")
    result = runner.invoke(app, ["target", "add", "qdrant", "--config", str(config), "--no-check"])
    assert result.exit_code == 1
    assert "failed validation" in result.output


def test_add_qdrant_check_against_unreachable_endpoint_fails_fast(
    runner: CliRunner, tmp_path: Path
) -> None:
    config = tmp_path / "qdrant-target.yml"
    config.write_text(_QDRANT_CONFIG, encoding="utf-8")
    result = runner.invoke(app, ["target", "add", "qdrant", "--config", str(config)])
    assert result.exit_code == 4
    assert "preflight failed" in result.output
    assert "reachable=False" in result.output
