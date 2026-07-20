"""Stable, content-derived record identifiers, per PROJECT_SPEC.md section 6.3.

Every ID is a prefixed multihash-style string: ``<prefix>_sha256_<base32>``,
where ``<base32>`` is the unpadded, lowercase RFC 4648 base32 encoding of
the raw 32-byte SHA-256 digest of the record's canonical identity input.
The identity input is an RFC 8785 canonical JSON array of exactly the
fields section 6.3 lists for that record type, in the order listed
there. IDs are never random (no UUIDs, per "UUID değil prefixed
multihash string kullanılabilir"): the same identity input always
derives the same ID, which is the entire point of a "stable" ID --
rebuilding the same source/parse/chunk/embedding/binding, even in a
different process or on a different machine, reproduces the same
portable identifier. A database is free to also carry an internal
UUIDv7, but that is never the identity that manifests, caches, or
cross-references use.

The chosen prefixes are:

- ``src`` -- ``source_id``
- ``ver`` -- ``source_version_id``
- ``prs`` -- ``parse_run_id``
- ``chk`` -- ``chunk_id`` (matches the example given directly in
  PROJECT_SPEC.md section 6.3: ``chk_sha256_<base32>``)
- ``emb`` -- ``embedding_id``
- ``idx`` -- ``index_binding_id``
"""

from __future__ import annotations

import base64

from ragledger.core.canonical import JSONValue, canonical_bytes
from ragledger.core.hashing import sha256_hex


def _base32_no_padding(digest: bytes) -> str:
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def _derive(prefix: str, *identity_fields: JSONValue) -> str:
    digest_hex = sha256_hex(canonical_bytes(list(identity_fields)))
    return f"{prefix}_sha256_{_base32_no_padding(bytes.fromhex(digest_hex))}"


def source_id(namespace: str, uri: str) -> str:
    """Derive ``source_id`` from the namespace and root-relative normalized URI.

    A rename is treated as a new logical source (a new ``source_id``);
    the relationship to the old one is recorded separately as a
    ``renamed_from`` relationship, not folded into identity.
    """
    return _derive("src", namespace, uri)


def source_version_id(source_id: str, content_hash: str) -> str:
    """Derive ``source_version_id`` from ``source_id`` and ``source_content_hash``."""
    return _derive("ver", source_id, content_hash)


def parse_run_id(source_version_id: str, parser_config_hash: str) -> str:
    """Derive ``parse_run_id`` from ``source_version_id`` and ``parser_config_hash``."""
    return _derive("prs", source_version_id, parser_config_hash)


def chunk_id(
    parse_run_id: str,
    chunker_config_hash: str,
    structural_locator: JSONValue,
    chunk_content_hash: str,
) -> str:
    """Derive ``chunk_id`` from parse run, chunker config, locator, and content hash.

    ``structural_locator`` should be the locator's canonical JSON-value
    form (for example a ``StructuralLocator`` model's
    ``model_dump(mode="json")``), not a pre-serialized string, so that
    field order never affects the derived ID.
    """
    return _derive("chk", parse_run_id, chunker_config_hash, structural_locator, chunk_content_hash)


def embedding_id(
    chunk_id: str,
    contextualized_text_hash: str,
    embedding_config_hash: str,
) -> str:
    """Derive ``embedding_id`` from chunk, contextualized text hash, and embedding config."""
    return _derive("emb", chunk_id, contextualized_text_hash, embedding_config_hash)


def index_binding_id(target: str, embedding_id: str, point_id: JSONValue) -> str:
    """Derive ``index_binding_id`` from the target alias, embedding id, and expected point id."""
    return _derive("idx", target, embedding_id, point_id)
