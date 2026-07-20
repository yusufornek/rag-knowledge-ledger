"""Tests for `ragledger.core.hashing`.

Checks the exact hash string format (lowercase, unpadded, 64-character
hex SHA-256, matching `docs/spec/manifest-v1.schema.json`'s
`sha256Hash` pattern), known SHA-256 test vectors, and the section 6.4
text normalization rules (NFC, CRLF/CR folded to LF by default,
trailing whitespace never touched).
"""

from __future__ import annotations

import re
import unicodedata

import pytest

from ragledger.core.hashing import (
    hash_canonical,
    hash_raw_bytes,
    hash_text,
    normalize_text,
    sha256_hex,
)

_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")

# Well-known SHA-256 test vectors (FIPS 180-4 / widely published: the
# digest of the empty string, and of the three-byte string "abc").
_KNOWN_VECTORS = [
    (b"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    (b"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
]


class TestHashFormat:
    @pytest.mark.parametrize("data", [b"", b"hello", "café".encode(), bytes(range(256))])
    def test_sha256_hex_matches_schema_pattern(self, data: bytes) -> None:
        digest = sha256_hex(data)
        assert _SHA256_HEX_RE.match(digest), digest

    def test_hash_canonical_matches_schema_pattern(self) -> None:
        assert _SHA256_HEX_RE.match(hash_canonical({"a": 1}))

    def test_hash_text_matches_schema_pattern(self) -> None:
        assert _SHA256_HEX_RE.match(hash_text("hello world"))


class TestKnownVectors:
    @pytest.mark.parametrize(("data", "expected"), _KNOWN_VECTORS)
    def test_sha256_hex_matches_published_vector(self, data: bytes, expected: str) -> None:
        assert sha256_hex(data) == expected

    def test_hash_raw_bytes_is_plain_sha256(self) -> None:
        data = b"raw source bytes, untouched"
        assert hash_raw_bytes(data) == sha256_hex(data)


class TestRawBytesUntouched:
    def test_hash_raw_bytes_does_not_normalize(self) -> None:
        nfc = "café".encode()
        nfd = unicodedata.normalize("NFD", "café").encode()
        assert nfc != nfd
        assert hash_raw_bytes(nfc) != hash_raw_bytes(nfd)


class TestTextNormalization:
    def test_nfc_and_nfd_text_hash_identically(self) -> None:
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        assert hash_text(nfc) == hash_text(nfd)

    def test_crlf_folded_to_lf_by_default(self) -> None:
        assert hash_text("line1\r\nline2") == hash_text("line1\nline2")

    def test_lone_cr_folded_to_lf_by_default(self) -> None:
        assert hash_text("line1\rline2") == hash_text("line1\nline2")

    def test_line_ending_normalization_can_be_disabled(self) -> None:
        with_crlf = hash_text("line1\r\nline2", normalize_line_endings=False)
        with_lf = hash_text("line1\nline2", normalize_line_endings=False)
        assert with_crlf != with_lf

    def test_trailing_whitespace_is_preserved(self) -> None:
        assert hash_text("hello   ") != hash_text("hello")

    def test_normalize_text_helper_matches_hash_text_input(self) -> None:
        raw = "line1\r\nline2  "
        normalized = normalize_text(raw)
        assert hash_text(raw) == sha256_hex(normalized.encode("utf-8"))


class TestHashCanonical:
    def test_hash_canonical_is_hash_of_canonical_bytes(self) -> None:
        from ragledger.core.canonical import canonical_bytes

        value = {"b": 1, "a": [1, 2, 3]}
        assert hash_canonical(value) == sha256_hex(canonical_bytes(value))

    def test_hash_canonical_is_order_independent_for_dict_input(self) -> None:
        assert hash_canonical({"a": 1, "b": 2}) == hash_canonical({"b": 2, "a": 1})

    def test_different_content_hashes_differently(self) -> None:
        assert hash_canonical({"a": 1}) != hash_canonical({"a": 2})
