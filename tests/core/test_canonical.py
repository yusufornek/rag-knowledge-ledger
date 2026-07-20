"""Tests for `ragledger.core.canonical`: RFC 8785 canonical JSON.

Covers Unicode normalization form equivalence, object key ordering
(including outside-the-BMP characters, which need UTF-16 surrogate-pair
comparison rather than plain code-point comparison), nested structures,
number formatting edge cases, minimal separators, and rejection of
values with no canonical JSON representation.
"""

from __future__ import annotations

import unicodedata

import pytest

from ragledger.core.canonical import canonical_bytes, canonicalize


class TestUnicodeNormalization:
    def test_nfc_and_nfd_input_produce_identical_bytes(self) -> None:
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        assert nfc != nfd  # sanity: the two forms really are different code points
        assert canonical_bytes(nfc) == canonical_bytes(nfd)

    def test_normalization_applies_to_object_keys_too(self) -> None:
        nfd_key = unicodedata.normalize("NFD", "café")
        nfc_key = unicodedata.normalize("NFC", "café")
        assert canonicalize({nfd_key: 1}) == canonicalize({nfc_key: 1})


class TestKeyOrdering:
    def test_object_keys_are_sorted(self) -> None:
        assert canonicalize({"b": 1, "a": 2, "c": 3}) == '{"a":2,"b":1,"c":3}'

    def test_key_order_is_independent_of_input_order(self) -> None:
        first = canonicalize({"zeta": 1, "alpha": 2})
        second = canonicalize({"alpha": 2, "zeta": 1})
        assert first == second == '{"alpha":2,"zeta":1}'

    def test_supplementary_plane_characters_sort_by_utf16_surrogate_pairs(self) -> None:
        # code point U+E000 (BMP, private use area) vs code point
        # U+10000 (supplementary plane, encoded in UTF-16 as the
        # surrogate pair 0xD800 0xDC00). Plain code-point comparison
        # says 0xE000 < 0x10000, so a naive sort would place `bmp_char`
        # first. RFC 8785 instead mandates UTF-16 code-unit comparison,
        # under which the supplementary character's leading surrogate
        # 0xD800 is less than 0xE000, so `supplementary_char` must sort
        # first -- the divergence a plain code-point sort would get
        # wrong.
        bmp_char = ""
        supplementary_char = "\U00010000"
        result = canonicalize({bmp_char: 1, supplementary_char: 2})
        assert result.index(supplementary_char) < result.index(bmp_char)


class TestNestedStructures:
    def test_nested_objects_and_arrays(self) -> None:
        value = {"list": [3, 1, {"z": 1, "a": 2}], "nested": {"b": [1, 2], "a": None}}
        result = canonicalize(value)
        assert result == '{"list":[3,1,{"a":2,"z":1}],"nested":{"a":null,"b":[1,2]}}'

    def test_array_order_is_preserved(self) -> None:
        assert canonicalize([3, 1, 2]) == "[3,1,2]"

    def test_empty_containers(self) -> None:
        assert canonicalize({}) == "{}"
        assert canonicalize([]) == "[]"
        assert canonicalize({"a": [], "b": {}}) == '{"a":[],"b":{}}'


class TestNumberFormatting:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0"),
            (-0, "0"),
            (1, "1"),
            (-1, "-1"),
            (100000000000000000000000, "100000000000000000000000"),
            (2**63, str(2**63)),
        ],
    )
    def test_integers_are_integer_safe(self, value: int, expected: str) -> None:
        assert canonicalize(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, "0"),
            (-0.0, "0"),
            (1.0, "1"),
            (-1.5, "-1.5"),
            (100.0, "100"),
            (0.5, "0.5"),
            (0.001, "0.001"),
            (1e20, "100000000000000000000"),
            (1e21, "1e+21"),
            (1e-6, "0.000001"),
            (1e-7, "1e-7"),
            (1.5e-5, "0.000015"),
            (123456789.123, "123456789.123"),
        ],
    )
    def test_floats_match_ecmascript_number_tostring(self, value: float, expected: str) -> None:
        assert canonicalize(value) == expected

    def test_bool_is_not_encoded_as_integer(self) -> None:
        assert canonicalize(True) == "true"
        assert canonicalize(False) == "false"
        assert canonicalize([True, 1]) == "[true,1]"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_nan_and_infinity_are_rejected(self, value: float) -> None:
        with pytest.raises(ValueError, match="NaN and Infinity"):
            canonicalize(value)


class TestMinimalSeparators:
    def test_no_whitespace_around_separators(self) -> None:
        assert canonicalize({"a": [1, 2], "b": 3}) == '{"a":[1,2],"b":3}'

    def test_no_trailing_newline(self) -> None:
        assert not canonical_bytes({"a": 1}).endswith(b"\n")

    def test_string_escaping_is_minimal(self) -> None:
        assert canonicalize('a"b\\c') == '"a\\"b\\\\c"'
        assert canonicalize("line1\nline2") == '"line1\\nline2"'
        assert canonicalize("\x01") == '"\\u0001"'

    def test_non_ascii_is_emitted_raw_not_escaped(self) -> None:
        assert canonicalize("café") == '"café"'


class TestRejectsNonJsonValues:
    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError):
            canonicalize(object())  # type: ignore[arg-type]

    def test_non_string_object_key_raises(self) -> None:
        # JSON object keys are always strings; a dict with a non-string
        # key can only arise from code that bypasses static typing
        # (JSONValue is keyed by str), but this must still fail loudly
        # rather than silently coercing or crashing unhelpfully. A tuple
        # of single characters is used here because it is hashable (so
        # it can be a dict key at all) and iterable-of-characters (so it
        # survives UTF-16 sort-key computation and reaches the explicit
        # type check inside `_encode_object`, rather than failing
        # earlier and less informatively during sorting).
        with pytest.raises(TypeError, match="object keys must be strings"):
            canonicalize({("a", "b"): "value"})  # type: ignore[dict-item]
