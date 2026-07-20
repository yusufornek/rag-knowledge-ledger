"""Manifest v1 assembly, schema validation, and canonical serialization.

This module owns the boundary between the typed `ragledger.core.models`
records and the on-the-wire manifest document: `build_manifest` turns a
set of already-constructed records into a schema-valid, hashed
`ManifestEnvelope`; `write_manifest`/`load_manifest` round-trip that
envelope through canonical JSON bytes on disk; and `signing_view_bytes`
is the shared primitive `ragledger.core.signing` uses to sign and verify
without duplicating the hashing rules defined here.

No function in this module reads the wall clock or generates random
values: `created_at` and every other timestamp are supplied by the
caller, so `build_manifest` called twice with the same records and the
same explicit timestamps returns byte-identical output (see
`canonical_manifest_bytes`). That determinism is the manifest's whole
purpose; see PROJECT_SPEC.md section 7.2 and `docs/architecture/adr/0002-signing-algorithm.md`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from ragledger.core.canonical import JSONValue, canonical_bytes
from ragledger.core.hashing import sha256_hex
from ragledger.core.models import (
    ArtifactRef,
    Assertion,
    BuildRecord,
    ChunkRecord,
    EmbeddingRecord,
    IndexBinding,
    Integrity,
    ManifestEnvelope,
    ParseRecord,
    QualityAssertion,
    SourceRecord,
    Statistics,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA_PATH = _REPO_ROOT / "docs" / "spec" / "manifest-v1.schema.json"
_PLACEHOLDER_HASH = "0" * 64


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    """Load and cache `docs/spec/manifest-v1.schema.json`.

    Resolved relative to this file's location in the repository rather
    than packaged with the wheel: the schema document lives under
    `docs/spec/` alongside the rest of the project's design documents,
    the same convention `tests/test_package.py` already relies on, and
    this module is only ever exercised from within a checkout of this
    repository (the CLI, tests, and this library all run in that
    context in v0.1.0).
    """
    with _SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema: dict[str, Any] = json.load(handle)
    return schema


def validate_manifest_document(data: Mapping[str, Any]) -> None:
    """Validate a raw manifest JSON document against manifest-v1.schema.json.

    Raises `jsonschema.exceptions.ValidationError` on the first schema
    violation found.
    """
    jsonschema.validate(instance=data, schema=_load_schema())


def manifest_to_dict(manifest: ManifestEnvelope) -> dict[str, JSONValue]:
    """Dump ``manifest`` to the plain JSON-value dict the schema and canonicalizer expect.

    Fields left unset (``None``) are omitted rather than emitted as
    JSON ``null``, because the schema's optional fields (for example
    `SourceRecord.discovered_by`) do not declare ``null`` as an allowed
    type; omitting an unknown value is also the correct reading of the
    "never fabricate, use `unknown`, or leave it out" convention
    documented in `ragledger.core.models`.
    """
    dumped: dict[str, JSONValue] = manifest.model_dump(
        mode="json", exclude_none=True, by_alias=True
    )
    return dumped


def signing_view_bytes(manifest: ManifestEnvelope) -> bytes:
    """Return the RFC 8785 canonical bytes of the normative signing view.

    Per PROJECT_SPEC.md section 11.1 and `docs/architecture/adr/0002-signing-algorithm.md`,
    the signing view is the manifest with `signatures` forced to an
    empty array and `integrity.manifest_hash` omitted entirely (not
    null, not a placeholder -- the key is absent), which is what avoids
    circularity between `manifest_hash` and the bytes it is a hash of.
    This is also the view `ragledger.core.signing.verify_manifest`
    recomputes from a signed manifest to check `hash_valid`.
    """
    view = manifest_to_dict(manifest)
    view["signatures"] = []
    integrity = dict(view["integrity"])  # type: ignore[arg-type]
    integrity.pop("manifest_hash", None)
    view["integrity"] = integrity
    return canonical_bytes(view)


def compute_manifest_hash(manifest: ManifestEnvelope) -> str:
    """Return the SHA-256 hex digest of ``manifest``'s signing view."""
    return sha256_hex(signing_view_bytes(manifest))


def canonical_manifest_bytes(manifest: ManifestEnvelope) -> bytes:
    """Return the final, fully-populated manifest's RFC 8785 canonical bytes.

    Unlike `signing_view_bytes`, this includes the real
    `integrity.manifest_hash` and whatever `signatures` the manifest
    carries. These are exactly the bytes `write_manifest` writes to
    disk, so a manifest file's own SHA-256 always matches what
    `compute_manifest_hash` would say about its content (modulo the
    hash covering the signing view, not the signed bytes themselves --
    see `ragledger.core.signing` for why that is not circular).
    """
    return canonical_bytes(manifest_to_dict(manifest))


def _compute_statistics(
    build: BuildRecord,
    sources: Sequence[SourceRecord],
    parse_runs: Sequence[ParseRecord],
    chunks: Sequence[ChunkRecord],
    embeddings: Sequence[EmbeddingRecord],
    index_bindings: Sequence[IndexBinding],
    assertions: Sequence[Assertion],
    artifacts: Sequence[ArtifactRef],
) -> Statistics:
    warning_count = len(build.warnings)
    warning_count += sum(len(parse_run.warnings) for parse_run in parse_runs)
    warning_count += sum(
        len(assertion.warnings)
        for assertion in assertions
        if isinstance(assertion, QualityAssertion)
    )
    return Statistics(
        source_count=len({source.id for source in sources}),
        source_version_count=len(sources),
        parse_run_count=len(parse_runs),
        chunk_count=len(chunks),
        embedding_count=len(embeddings),
        index_binding_count=len(index_bindings),
        assertion_count=len(assertions),
        artifact_count=len(artifacts),
        warning_count=warning_count,
    )


def build_manifest(
    *,
    namespace: str,
    created_at: datetime,
    build: BuildRecord,
    ledger_version: str,
    sources: Sequence[SourceRecord] = (),
    parse_runs: Sequence[ParseRecord] = (),
    chunks: Sequence[ChunkRecord] = (),
    embeddings: Sequence[EmbeddingRecord] = (),
    index_bindings: Sequence[IndexBinding] = (),
    assertions: Sequence[Assertion] = (),
    artifacts: Sequence[ArtifactRef] = (),
    extensions: dict[str, Any] | None = None,
) -> ManifestEnvelope:
    """Assemble a schema-valid, hashed `ManifestEnvelope` from its records.

    `statistics` is computed from the record lists (never supplied by
    the caller); `integrity.manifest_hash` is computed from the
    resulting signing view (see `signing_view_bytes`). `signatures`
    starts empty -- an unsigned manifest is a valid manifest, per
    PROJECT_SPEC.md section 10.2 ("Signing fail: unsigned manifest
    artifact olabilir"); attach signatures afterward with
    `ragledger.core.signing.sign_manifest`.

    The returned manifest is validated against
    `docs/spec/manifest-v1.schema.json` before being returned.
    """
    statistics = _compute_statistics(
        build, sources, parse_runs, chunks, embeddings, index_bindings, assertions, artifacts
    )
    provisional = ManifestEnvelope(
        created_at=created_at,
        ledger_version=ledger_version,
        namespace=namespace,
        build=build,
        sources=list(sources),
        parse_runs=list(parse_runs),
        chunks=list(chunks),
        embeddings=list(embeddings),
        index_bindings=list(index_bindings),
        assertions=list(assertions),
        artifacts=list(artifacts),
        statistics=statistics,
        integrity=Integrity(manifest_hash=_PLACEHOLDER_HASH),
        signatures=[],
        extensions=extensions,
    )
    final = provisional.model_copy(
        update={"integrity": Integrity(manifest_hash=compute_manifest_hash(provisional))}
    )
    validate_manifest_document(manifest_to_dict(final))
    return final


def write_manifest(path: Path, manifest: ManifestEnvelope) -> None:
    """Validate ``manifest`` against the schema and write it as canonical JSON bytes."""
    validate_manifest_document(manifest_to_dict(manifest))
    Path(path).write_bytes(canonical_manifest_bytes(manifest))


def load_manifest(path: Path) -> ManifestEnvelope:
    """Read, schema-validate, and parse a manifest JSON file."""
    data = json.loads(Path(path).read_bytes())
    validate_manifest_document(data)
    return ManifestEnvelope.model_validate(data)
