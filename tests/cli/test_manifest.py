"""Tests for `ragledger manifest validate|sign|verify`."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from ragledger.cli import app


def _build(
    runner: CliRunner,
    corpus_dir: Path,
    tmp_path: Path,
    write_minimal_config: Callable[..., Path],
    *,
    name: str = "manifest.json",
) -> Path:
    config = write_minimal_config(tmp_path / "ragledger.yml", root=corpus_dir)
    output = tmp_path / name
    result = runner.invoke(
        app,
        [
            "build",
            str(corpus_dir),
            "--config",
            str(config),
            "--output",
            str(output),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--cache",
            str(tmp_path / "cache"),
            "--epoch",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    return output


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def test_validate_a_freshly_built_manifest_succeeds(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    result = runner.invoke(app, ["manifest", "validate", str(manifest)])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_validate_missing_file_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["manifest", "validate", str(tmp_path / "nope.json")])
    assert result.exit_code == 1


def test_validate_invalid_json_is_a_config_error(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["manifest", "validate", str(bad)])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_validate_schema_invalid_document_is_a_config_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a manifest"}), encoding="utf-8")
    result = runner.invoke(app, ["manifest", "validate", str(bad)])
    assert result.exit_code == 1
    assert "schema validation" in result.output


def test_validate_corrupted_content_hash_is_an_integrity_failure(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    data = json.loads(manifest.read_bytes())
    data["namespace"] = "tampered-namespace"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = runner.invoke(app, ["manifest", "validate", str(manifest)])
    assert result.exit_code == 5
    assert "does not match" in result.output


# --------------------------------------------------------------------------
# sign / verify
# --------------------------------------------------------------------------


def _generate_key(runner: CliRunner, tmp_path: Path, name: str = "signing") -> tuple[Path, Path]:
    private_key = tmp_path / f"{name}.key"
    public_key = tmp_path / f"{name}.pub"
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
    return private_key, public_key


def test_sign_then_verify_with_trusted_key_is_valid_trusted(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    private_key, public_key = _generate_key(runner, tmp_path)

    sign_result = runner.invoke(
        app, ["manifest", "sign", str(manifest), "--key-file", str(private_key), "--issuer", "ci"]
    )
    assert sign_result.exit_code == 0, sign_result.output

    verify_result = runner.invoke(
        app, ["manifest", "verify", str(manifest), "--public-key", str(public_key)]
    )
    assert verify_result.exit_code == 0, verify_result.output
    assert "VALID_TRUSTED" in verify_result.output


def test_verify_with_unrecognized_key_is_valid_untrusted(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    private_key, _ = _generate_key(runner, tmp_path, name="signer")
    _, other_public_key = _generate_key(runner, tmp_path, name="other")

    runner.invoke(app, ["manifest", "sign", str(manifest), "--key-file", str(private_key)])
    result = runner.invoke(
        app, ["manifest", "verify", str(manifest), "--public-key", str(other_public_key)]
    )
    assert result.exit_code == 2
    assert "VALID_UNTRUSTED" in result.output


def test_verify_tampered_manifest_is_invalid(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    private_key, public_key = _generate_key(runner, tmp_path)
    runner.invoke(app, ["manifest", "sign", str(manifest), "--key-file", str(private_key)])

    data = json.loads(manifest.read_bytes())
    data["namespace"] = "tampered-after-signing"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = runner.invoke(
        app, ["manifest", "verify", str(manifest), "--public-key", str(public_key)]
    )
    assert result.exit_code == 5
    assert "INVALID" in result.output


def test_verify_unsigned_manifest_is_incomplete(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    result = runner.invoke(app, ["manifest", "verify", str(manifest)])
    assert result.exit_code == 5
    assert "INCOMPLETE" in result.output


def test_verify_deep_succeeds_when_artifacts_are_intact(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    private_key, public_key = _generate_key(runner, tmp_path)
    runner.invoke(app, ["manifest", "sign", str(manifest), "--key-file", str(private_key)])

    result = runner.invoke(
        app,
        [
            "manifest",
            "verify",
            str(manifest),
            "--public-key",
            str(public_key),
            "--deep",
            "--artifacts",
            str(tmp_path / "artifacts"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "artifact(s) OK" in result.output


def test_verify_deep_fails_when_an_artifact_is_missing(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    private_key, public_key = _generate_key(runner, tmp_path)
    runner.invoke(app, ["manifest", "sign", str(manifest), "--key-file", str(private_key)])

    empty_artifacts = tmp_path / "empty-artifacts"
    empty_artifacts.mkdir()
    result = runner.invoke(
        app,
        [
            "manifest",
            "verify",
            str(manifest),
            "--public-key",
            str(public_key),
            "--deep",
            "--artifacts",
            str(empty_artifacts),
        ],
    )
    assert result.exit_code == 5
    assert "deep artifact verification failed" in result.output


def test_sign_with_missing_key_file_is_a_config_error(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    result = runner.invoke(
        app,
        ["manifest", "sign", str(manifest), "--key-file", str(tmp_path / "no-such-key")],
    )
    assert result.exit_code == 1
    assert "cannot read private key" in result.output


def test_sign_writes_to_explicit_output_leaving_original_untouched(
    runner: CliRunner, corpus_dir: Path, tmp_path: Path, write_minimal_config: Callable[..., Path]
) -> None:
    manifest = _build(runner, corpus_dir, tmp_path, write_minimal_config)
    original_bytes = manifest.read_bytes()
    private_key, _ = _generate_key(runner, tmp_path)
    signed_output = tmp_path / "manifest-signed.json"

    result = runner.invoke(
        app,
        [
            "manifest",
            "sign",
            str(manifest),
            "--key-file",
            str(private_key),
            "--output",
            str(signed_output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert manifest.read_bytes() == original_bytes
    assert signed_output.is_file()
