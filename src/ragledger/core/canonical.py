"""RFC 8785 (JSON Canonicalization Scheme) serialization.

This module implements canonical JSON encoding per the design specification
sections 6.4 and 7.2, and `docs/spec/manifest-v1.schema.json`'s
`integrity.canonicalization` value of ``"RFC8785"``:

- UTF-8 output, no byte order mark.
- Object member names sorted by UTF-16 code unit sequence (the ordering
  RFC 8785 mandates, which matches ECMAScript's default string
  comparison and therefore what ``JSON.stringify`` with sorted keys
  would produce).
- Minimal separators: ``,`` and ``:`` with no surrounding whitespace,
  and no trailing newline.
- Numbers formatted per the ECMAScript ``Number::toString`` algorithm
  (RFC 8785 section 3.2.2.3), so that a given IEEE-754 double always
  serializes to exactly one canonical digit string. Python ``int``
  values are integer-safe: they are formatted with plain decimal
  ``str()`` and never routed through float formatting, so no precision
  is lost regardless of magnitude.
- All string values (including object keys) are Unicode-normalized to
  NFC before encoding, per the design specification section 6.4. This is an
  intentional extension beyond plain RFC 8785 (which is silent on
  Unicode normalization): it guarantees that two Python strings which
  are canonically equivalent but differ in normalization form (for
  example NFC vs. NFD input from different filesystems or editors)
  always produce byte-identical canonical output and hashes.
- Escaping is minimal: only ``"``, ``\\``, and control characters
  (U+0000-U+001F) are escaped; non-ASCII characters are emitted as raw
  UTF-8 bytes, never as ``\\uXXXX`` sequences.

``canonical_bytes`` is the single function used both to compute content
hashes (see ``ragledger.core.hashing``) and to write manifests to disk
(see ``ragledger.core.manifest``): the exact same bytes are hashed and
persisted, so a manifest file's SHA-256 on disk always matches the hash
that was computed for it.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Union

JSONValue = Union[None, bool, int, float, str, "list[JSONValue]", "dict[str, JSONValue]"]
"""A value that RFC 8785 canonicalization knows how to encode.

This mirrors the shape produced by ``pydantic.BaseModel.model_dump(mode="json")``:
plain ``dict``/``list`` containers and JSON scalar leaves. It is a
recursive alias; mypy understands it as such.
"""

_CONTROL_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

_EXPONENT_RE = re.compile(r"^(?P<mantissa>\d+(?:\.\d+)?)[eE](?P<exponent>[+-]?\d+)$")


def canonicalize(value: JSONValue) -> str:
    """Return the RFC 8785 canonical JSON text for ``value``."""
    return _encode(value)


def canonical_bytes(value: JSONValue) -> bytes:
    """Return the UTF-8 encoded RFC 8785 canonical JSON bytes for ``value``.

    These are exactly the bytes that are hashed for content identity and
    the bytes that are written to a manifest file on disk; no trailing
    newline is added.
    """
    return canonicalize(value).encode("utf-8")


def _encode(value: JSONValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        return _encode_object(value)
    raise TypeError(f"value of type {type(value)!r} is not canonical-JSON serializable")


def _encode_object(value: dict[str, JSONValue]) -> str:
    members = []
    for key in sorted(value.keys(), key=_utf16_code_units):
        if not isinstance(key, str):
            raise TypeError(f"object keys must be strings, got {type(key)!r}")
        members.append(_encode_string(key) + ":" + _encode(value[key]))
    return "{" + ",".join(members) + "}"


def _encode_string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    parts = ['"']
    for char in normalized:
        if char in _CONTROL_ESCAPES:
            parts.append(_CONTROL_ESCAPES[char])
        elif char < " ":
            parts.append(f"\\u{ord(char):04x}")
        else:
            parts.append(char)
    parts.append('"')
    return "".join(parts)


def _encode_float(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        raise ValueError("NaN and Infinity have no canonical JSON representation")
    if value == 0.0:
        # Canonicalizes both +0.0 and -0.0 to "0", matching ECMAScript's
        # Number::toString, which never emits a sign for zero.
        return "0"
    sign = "-" if value < 0 else ""
    digits, point = _shortest_digits_and_point(-value if value < 0 else value)
    return sign + _format_digits(digits, point)


def _shortest_digits_and_point(value: float) -> tuple[str, int]:
    """Return (significant_digits, point) for the shortest round-trip decimal.

    ``point`` is the ECMA-262 ``n``: the significant digits, read as an
    integer ``s`` with ``k`` digits, satisfy ``value == s * 10 ** (point - k)``.

    Python's ``repr(float)`` already produces the shortest decimal string
    that round-trips to the original double (the same guarantee the
    ECMAScript algorithm relies on); this only reformats that string's
    digits and decimal-point position into the (digits, point) shape the
    ECMA-262 rendering rules in ``_format_digits`` expect.
    """
    text = repr(value)
    match = _EXPONENT_RE.match(text)
    if match:
        mantissa = match.group("mantissa")
        exponent = int(match.group("exponent"))
    else:
        mantissa = text
        exponent = 0
    int_part, _, frac_part = mantissa.partition(".")
    full_digits = int_part + frac_part
    point = len(int_part) + exponent

    stripped_leading = full_digits.lstrip("0")
    leading_zeros = len(full_digits) - len(stripped_leading)
    significant = stripped_leading.rstrip("0") or "0"
    return significant, point - leading_zeros


def _format_digits(digits: str, point: int) -> str:
    """Render (digits, point) per ECMA-262 Number::toString, section 7.1.12.1."""
    k = len(digits)
    if k <= point <= 21:
        return digits + "0" * (point - k)
    if 0 < point <= 21:
        return digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return "0." + "0" * (-point) + digits
    exponent = point - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    exponent_sign = "+" if exponent >= 0 else "-"
    return f"{mantissa}e{exponent_sign}{abs(exponent)}"


def _utf16_code_units(text: str) -> tuple[int, ...]:
    """Encode ``text`` as the sequence of UTF-16 code units RFC 8785 sorts by.

    Characters outside the Basic Multilingual Plane are represented as
    surrogate pairs, exactly as UTF-16 (and therefore ECMAScript string
    comparison) would represent them; a plain code-point sort would
    order such characters differently.
    """
    units: list[int] = []
    for char in text:
        code_point = ord(char)
        if code_point > 0xFFFF:
            code_point -= 0x10000
            units.append(0xD800 + (code_point >> 10))
            units.append(0xDC00 + (code_point & 0x3FF))
        else:
            units.append(code_point)
    return tuple(units)
