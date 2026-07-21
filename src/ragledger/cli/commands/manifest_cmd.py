"""`ragledger manifest validate|sign|verify`, per PROJECT_SPEC.md sections 17.1, 11, and 33.6.

Exit code interpretation for `verify` (there is no single spec row that
enumerates all four `VerificationOverall` outcomes against the section
17.1 exit table, so this is a documented choice -- see
`docs/reviews/m4-status-notes.md`):

- `VALID_TRUSTED` -> `0`.
- `VALID_UNTRUSTED` -> `2` ("Findings var, gate fail değil"): the
  content hash is intact and the signature is cryptographically valid,
  just from a key this caller does not recognize -- worth flagging, not
  a tamper/integrity failure.
- `INVALID` or `INCOMPLETE` -> `5` ("Signature/integrity failure"):
  either the content was tampered with, or there is no valid signature
  to establish authenticity from at all.
- A `--deep` artifact-hash mismatch always forces `5`, regardless of
  the signature outcome, since that is squarely a content integrity
  failure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import typer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from ragledger.cli._build_support import resolve_epoch
from ragledger.cli._config import ConfigError
from ragledger.cli._exit import (
    EXIT_CONFIG_ERROR,
    EXIT_FINDINGS,
    EXIT_INTEGRITY_FAILURE,
    CliError,
    run_command,
)
from ragledger.cli._output import log
from ragledger.core.artifacts import ArtifactStore
from ragledger.core.manifest import (
    compute_manifest_hash,
    validate_manifest_document,
    write_manifest,
)
from ragledger.core.models import ManifestEnvelope
from ragledger.core.signing import (
    VerificationOverall,
    fingerprint,
    read_private_key,
    read_public_key,
    sign_manifest,
    verify_manifest,
)

app = typer.Typer(help="Inspect, sign, and verify manifest files.", no_args_is_help=True)


def _load_manifest_document(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CliError(f"cannot read manifest {path}: {exc}", exit_code=EXIT_CONFIG_ERROR) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}", exit_code=EXIT_CONFIG_ERROR) from exc
    if not isinstance(data, dict):
        raise CliError(f"{path} must contain a JSON object", exit_code=EXIT_CONFIG_ERROR)
    return data


def _load_manifest_envelope(path: Path) -> ManifestEnvelope:
    data = _load_manifest_document(path)
    try:
        validate_manifest_document(data)
    except jsonschema.exceptions.ValidationError as exc:
        raise CliError(
            f"{path} failed schema validation: {exc.message}", exit_code=EXIT_CONFIG_ERROR
        ) from exc
    try:
        return ManifestEnvelope.model_validate(data)
    except ValidationError as exc:
        raise CliError(
            f"{path} failed manifest model validation: {exc}", exit_code=EXIT_CONFIG_ERROR
        ) from exc


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


@app.command("validate")
def validate(
    manifest: Path = typer.Argument(..., help="Path to the manifest JSON file."),  # noqa: B008
) -> None:
    """Schema- and integrity-validate a manifest file."""
    run_command(lambda: _validate_impl(manifest))


def _validate_impl(manifest_path: Path) -> None:
    envelope = _load_manifest_envelope(manifest_path)
    recomputed = compute_manifest_hash(envelope)
    if recomputed != envelope.integrity.manifest_hash:
        raise CliError(
            f"{manifest_path}: integrity.manifest_hash {envelope.integrity.manifest_hash!r} "
            f"does not match the recomputed signing-view hash {recomputed!r} (content was "
            "modified after signing/build, or the file was corrupted)",
            exit_code=EXIT_INTEGRITY_FAILURE,
        )
    log(
        f"{manifest_path}: valid. namespace={envelope.namespace!r} "
        f"build_status={envelope.build.status} sources={envelope.statistics.source_count} "
        f"chunks={envelope.statistics.chunk_count} "
        f"embeddings={envelope.statistics.embedding_count} "
        f"signatures={len(envelope.signatures)} manifest_hash={envelope.integrity.manifest_hash}"
    )


# --------------------------------------------------------------------------
# sign
# --------------------------------------------------------------------------


@app.command("sign")
def sign(
    manifest: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the manifest JSON file to sign."
    ),
    key_file: Path = typer.Option(  # noqa: B008
        ...,
        "--key-file",
        help="Path to a raw Ed25519 private key file (see `ragledger key generate`).",
    ),
    output: Path | None = typer.Option(  # noqa: B008
        None, "--output", help="Where to write the signed manifest (default: overwrite in place)."
    ),
    issuer: str | None = typer.Option(
        None, "--issuer", help="Optional signer identity to record on the signature."
    ),
    epoch: int | None = typer.Option(
        None,
        "--epoch",
        help="Unix timestamp for signed_at; falls back to SOURCE_DATE_EPOCH, then current time.",
    ),
) -> None:
    """Attach an Ed25519 signature to a manifest, per PROJECT_SPEC.md section 11.1."""
    run_command(lambda: _sign_impl(manifest, key_file, output, issuer, epoch))


def _sign_impl(
    manifest_path: Path, key_file: Path, output: Path | None, issuer: str | None, epoch: int | None
) -> None:
    envelope = _load_manifest_envelope(manifest_path)
    try:
        private_key = read_private_key(key_file)
    except (OSError, ValueError) as exc:
        raise CliError(
            f"cannot read private key {key_file}: {exc}", exit_code=EXIT_CONFIG_ERROR
        ) from exc

    try:
        resolved_epoch = resolve_epoch(epoch)
    except ConfigError as exc:
        raise CliError(str(exc), exit_code=EXIT_CONFIG_ERROR) from exc
    signed_at = (
        datetime.fromtimestamp(resolved_epoch, tz=UTC)
        if resolved_epoch is not None
        else datetime.now(UTC)
    )
    signed = sign_manifest(envelope, private_key, signed_at=signed_at, issuer=issuer)

    destination = output if output is not None else manifest_path
    write_manifest(destination, signed)
    key_id = signed.signatures[-1].key_id
    log(
        f"wrote {destination}: signed with key_id={key_id} issuer={issuer!r} "
        f"signed_at={signed_at.isoformat()}"
    )


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


@app.command("verify")
def verify(
    manifest: Path = typer.Argument(..., help="Path to the manifest JSON file."),  # noqa: B008
    public_key: list[Path] = typer.Option(  # noqa: B008
        [], "--public-key", help="A trusted Ed25519 public key file. Repeatable."
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Also verify every artifact's stored bytes against its declared hash.",
    ),
    artifacts_dir: Path = typer.Option(  # noqa: B008
        Path(".ragledger/artifacts"),
        "--artifacts",
        help="Artifact store root for --deep verification.",
    ),
) -> None:
    """Verify a manifest's signatures and content hash, per PROJECT_SPEC.md section 33.6."""
    run_command(lambda: _verify_impl(manifest, public_key, deep, artifacts_dir))


def _verify_impl(
    manifest_path: Path, public_key_paths: list[Path], deep: bool, artifacts_dir: Path
) -> None:
    envelope = _load_manifest_envelope(manifest_path)

    trusted_keys: dict[str, Ed25519PublicKey] = {}
    for key_path in public_key_paths:
        try:
            key = read_public_key(key_path)
        except (OSError, ValueError) as exc:
            raise CliError(
                f"cannot read public key {key_path}: {exc}", exit_code=EXIT_CONFIG_ERROR
            ) from exc
        trusted_keys[fingerprint(key)] = key

    result = verify_manifest(envelope, trusted_keys)
    for sig in result.signatures:
        log(f"signature key_id={sig.key_id} status={sig.status.value}")
    log(
        f"overall={result.overall.value} hash_valid={result.hash_valid} "
        f"signatures={len(result.signatures)}"
    )

    deep_failures: list[str] = []
    if deep:
        store = ArtifactStore(artifacts_dir)
        for artifact in envelope.artifacts:
            if not store.verify(artifact.sha256):
                deep_failures.append(artifact.artifact_id)
        if deep_failures:
            log(
                f"deep verification: {len(deep_failures)} artifact(s) failed: "
                f"{', '.join(deep_failures[:10])}"
            )
        else:
            log(f"deep verification: {len(envelope.artifacts)} artifact(s) OK")

    if deep_failures:
        raise CliError(
            f"deep artifact verification failed for {len(deep_failures)} artifact(s)",
            exit_code=EXIT_INTEGRITY_FAILURE,
        )
    if result.overall == VerificationOverall.VALID_TRUSTED:
        return
    if result.overall == VerificationOverall.VALID_UNTRUSTED:
        raise CliError(
            f"{manifest_path}: manifest signature is cryptographically valid but signed by an "
            "untrusted key",
            exit_code=EXIT_FINDINGS,
        )
    raise CliError(
        f"{manifest_path}: manifest verification failed: {result.overall.value}",
        exit_code=EXIT_INTEGRITY_FAILURE,
    )
