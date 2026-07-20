"""Content-derived identifiers for governance assertion records.

Mirrors the style `ragledger.core.ids` uses for the six lineage record
types (`<prefix>_sha256_<base32>`) for the assertion record types that
module does not cover: `ragledger.core.ids` only derives
source/version/parse-run/chunk/embedding/index-binding IDs, never
assertion IDs. This reimplements the same well-documented format
independently, built only from `ragledger.core`'s public canonical-JSON
and hashing functions (`canonical_bytes`, `sha256_hex`) -- not by
importing `ragledger.core.ids`'s private helpers -- so assertion IDs are
just as deterministic and content-addressed (never random, never a
UUID) as every other record ID in this project.
"""

from __future__ import annotations

import base64

from ragledger.core.canonical import JSONValue, canonical_bytes
from ragledger.core.hashing import sha256_hex


def derive_assertion_id(prefix: str, *identity_fields: JSONValue) -> str:
    """Derive a stable `<prefix>_sha256_<base32>` id from canonical identity fields."""
    digest_hex = sha256_hex(canonical_bytes(list(identity_fields)))
    encoded = base64.b32encode(bytes.fromhex(digest_hex)).decode("ascii").rstrip("=").lower()
    return f"{prefix}_sha256_{encoded}"
