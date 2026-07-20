"""SHA-256 content hashing, per PROJECT_SPEC.md section 6.2 and 6.4.

Every hash string produced here is a lowercase, unpadded, 64-character
hexadecimal SHA-256 digest, matching the ``sha256Hash`` pattern
``^[a-f0-9]{64}$`` used throughout `docs/spec/manifest-v1.schema.json`
(for example ``content_hash``, ``parser_config_hash``,
``chunk_content_hash``, and ``manifest_hash``). Raw content hashes carry
no prefix and are not multihash-wrapped; the prefixed multihash-style
strings (``chk_sha256_<base32>`` and similar) are a separate, ID-layer
convention implemented in ``ragledger.core.ids``.

Section 6.2 distinguishes hashes by what they are computed over:

- Raw bytes, untouched (``source_content_hash``): use ``hash_raw_bytes``.
- Normalized UTF-8 text (``chunk_content_hash``, ``contextualized_text_hash``):
  use ``hash_text``, which applies the section 6.4 normalization rule.
- A canonical JSON value -- a config object or a normalized document
  (``parser_config_hash``, ``parsed_document_hash``, ``chunker_config_hash``,
  ``embedding_config_hash``, ``payload_hash``, and the manifest's own
  ``manifest_hash``): use ``hash_canonical``, which is the RFC 8785
  canonicalization from ``ragledger.core.canonical`` composed with
  SHA-256.
"""

from __future__ import annotations

import hashlib
import unicodedata

from ragledger.core.canonical import JSONValue, canonical_bytes

HASH_ALGORITHM = "sha256"


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def hash_raw_bytes(data: bytes) -> str:
    """Hash raw bytes exactly as-is.

    Per section 6.4, "raw source hash bytes'a dokunmaz" (raw source
    hashing never touches the bytes): no normalization of any kind is
    applied here. This is the function behind ``source_content_hash``.
    """
    return sha256_hex(data)


def normalize_text(text: str, *, normalize_line_endings: bool = True) -> str:
    """Apply the section 6.4 text normalization rule to ``text``.

    Unicode NFC normalization is always applied. CRLF and lone-CR line
    endings are folded to LF when ``normalize_line_endings`` is true,
    which is the spec's stated default ("default yalnız line-ending
    normalization'dır"). Trailing whitespace is deliberately never
    touched here: whether it is significant is a chunker configuration
    decision made by the caller, and the spec requires that the text the
    chunker sees and the text that gets hashed be identical ("Chunker'ın
    gördüğü text ile hash text aynı olmalıdır").
    """
    normalized = unicodedata.normalize("NFC", text)
    if normalize_line_endings:
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return normalized


def hash_text(text: str, *, normalize_line_endings: bool = True) -> str:
    """Hash normalized UTF-8 text content.

    Used for ``chunk_content_hash`` (raw chunk text) and
    ``contextualized_text_hash`` (the exact text sent to the embedder).
    The caller is responsible for passing the exact text the downstream
    stage observed; this function applies only the section 6.4
    normalization (see ``normalize_text``) and never anything more
    aggressive, so the hash never silently diverges from what was
    actually processed.
    """
    normalized = normalize_text(text, normalize_line_endings=normalize_line_endings)
    return sha256_hex(normalized.encode("utf-8"))


def hash_canonical(value: JSONValue) -> str:
    """Hash the RFC 8785 canonical JSON encoding of ``value``.

    Used for the composite/config hashes in section 6.2 whose input is a
    structured object rather than raw bytes or plain text:
    ``parser_config_hash``, ``parsed_document_hash``, ``chunker_config_hash``,
    ``embedding_config_hash``, ``payload_hash``, and ``manifest_hash``.
    """
    return sha256_hex(canonical_bytes(value))
