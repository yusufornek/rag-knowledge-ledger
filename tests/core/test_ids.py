"""Tests for `ragledger.core.ids`: stable, content-derived record identifiers.

Checks the `<prefix>_sha256_<base32>` format, that IDs are pure
functions of their declared inputs (same input -> same ID, on any run,
in any process), that changing any one input field changes the ID, and
pins a handful of exact values as regression vectors so an accidental
change to the derivation algorithm (for example, reordering fields, or
switching the base32 alphabet) is caught even if it happens to still be
internally self-consistent.
"""

from __future__ import annotations

import re

import pytest

from ragledger.core import ids

_ID_RE = re.compile(r"^(?P<prefix>[a-z]{3})_sha256_[a-z2-7]+$")


class TestIdFormat:
    def test_source_id_matches_prefixed_multihash_format(self) -> None:
        value = ids.source_id("ns", "file:a.md")
        match = _ID_RE.match(value)
        assert match is not None, value
        assert match.group("prefix") == "src"

    @pytest.mark.parametrize(
        ("builder", "expected_prefix"),
        [
            (lambda: ids.source_id("ns", "file:a.md"), "src"),
            (lambda: ids.source_version_id("src_x", "a" * 64), "ver"),
            (lambda: ids.parse_run_id("ver_x", "a" * 64), "prs"),
            (
                lambda: ids.chunk_id("prs_x", "a" * 64, {"kind": "document_span"}, "b" * 64),
                "chk",
            ),
            (lambda: ids.embedding_id("chk_x", "a" * 64, "b" * 64), "emb"),
            (lambda: ids.index_binding_id("target", "emb_x", "point-1"), "idx"),
        ],
    )
    def test_every_record_kind_has_its_documented_prefix(self, builder, expected_prefix) -> None:
        value = builder()
        assert value.startswith(f"{expected_prefix}_sha256_")
        assert _ID_RE.match(value)

    def test_ids_are_never_uuid_shaped(self) -> None:
        value = ids.source_id("ns", "file:a.md")
        uuid_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
        )
        assert uuid_re.match(value) is None


class TestDeterminism:
    def test_source_id_is_a_pure_function_of_its_inputs(self) -> None:
        first = ids.source_id("example-support-kb", "file:documents/refund.pdf")
        second = ids.source_id("example-support-kb", "file:documents/refund.pdf")
        assert first == second

    def test_full_chain_is_deterministic_across_two_derivations(self) -> None:
        def derive_chain() -> tuple[str, str, str, str, str, str]:
            source_id = ids.source_id("ns", "file:doc.md")
            version_id = ids.source_version_id(source_id, "a" * 64)
            run_id = ids.parse_run_id(version_id, "b" * 64)
            locator = {"kind": "document_span", "ordinal": 0}
            chk_id = ids.chunk_id(run_id, "c" * 64, locator, "d" * 64)
            emb_id = ids.embedding_id(chk_id, "e" * 64, "f" * 64)
            binding_id = ids.index_binding_id("primary", emb_id, "point-1")
            return source_id, version_id, run_id, chk_id, emb_id, binding_id

        assert derive_chain() == derive_chain()


class TestInputSensitivity:
    def test_different_namespace_changes_source_id(self) -> None:
        assert ids.source_id("ns-a", "file:a.md") != ids.source_id("ns-b", "file:a.md")

    def test_different_uri_changes_source_id(self) -> None:
        assert ids.source_id("ns", "file:a.md") != ids.source_id("ns", "file:b.md")

    def test_different_content_hash_changes_source_version_id(self) -> None:
        a = ids.source_version_id("src_x", "a" * 64)
        b = ids.source_version_id("src_x", "b" * 64)
        assert a != b

    def test_different_locator_changes_chunk_id(self) -> None:
        parse_run_id = "prs_x"
        chunker_config_hash = "a" * 64
        chunk_content_hash = "b" * 64
        locator_a = {"kind": "document_span", "ordinal": 0}
        locator_b = {"kind": "document_span", "ordinal": 1}
        id_a = ids.chunk_id(parse_run_id, chunker_config_hash, locator_a, chunk_content_hash)
        id_b = ids.chunk_id(parse_run_id, chunker_config_hash, locator_b, chunk_content_hash)
        assert id_a != id_b

    def test_different_point_id_changes_index_binding_id(self) -> None:
        a = ids.index_binding_id("target", "emb_x", "point-1")
        b = ids.index_binding_id("target", "emb_x", "point-2")
        assert a != b

    def test_locator_field_order_does_not_matter(self) -> None:
        # The locator is hashed as canonical JSON, so key order in the
        # Python dict passed in must not affect the derived chunk_id.
        locator_a = {"kind": "document_span", "ordinal": 0, "page_start": 1}
        locator_b = {"page_start": 1, "ordinal": 0, "kind": "document_span"}
        id_a = ids.chunk_id("prs_x", "a" * 64, locator_a, "b" * 64)
        id_b = ids.chunk_id("prs_x", "a" * 64, locator_b, "b" * 64)
        assert id_a == id_b


class TestRegressionVectors:
    """Pinned exact outputs. If these change, the derivation algorithm
    changed -- which may be intentional, but must never happen silently.
    """

    def test_source_id_pinned_vector(self) -> None:
        value = ids.source_id("example-support-kb", "file:documents/refund.pdf")
        assert value == "src_sha256_a7xz2hkxzaihzwq3e46lxhbhablgmthozzeagxflzcmk36l3e52a"

    def test_chunk_id_pinned_vector(self) -> None:
        locator = {"kind": "document_span", "ordinal": 0, "page_start": 1, "page_end": 1}
        value = ids.chunk_id(
            "prs_sha256_aaaa",
            "b" * 64,
            locator,
            "c" * 64,
        )
        assert value == "chk_sha256_53iiid4d6vng7gqjbjxpyuaoiq7o2jt7rirt6zqa37lvw2rqod7q"
