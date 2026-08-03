"""RFC 9562 UUID version 7 generation.

PROJECT_SPEC.md section 15.3 requires every table's internal primary
key to be a UUIDv7 ("Internal UUIDv7; portable identity unique
text/binary hash indexed" -- the *portable* identity is the separate,
content-derived `ragledger.core.ids` string, not this key). Python's
standard library `uuid` module does not generate version 7 UUIDs on
the 3.11/3.12/3.13 interpreters this project supports, so this module
implements the layout directly: a 48-bit millisecond Unix timestamp,
the fixed 4-bit version field, 12 bits of randomness, the fixed 2-bit
variant field, and 62 more bits of randomness, exactly as the RFC
defines the "fixed-length dedicated counter" -free layout (the
simplest conformant construction; this project has no need for the
monotonic-counter variant since IDs are never compared for ordering
within the same millisecond).
"""

from __future__ import annotations

import os
import time
import uuid

_VERSION_7 = 0x7
_VARIANT_10 = 0b10


def uuid7() -> uuid.UUID:
    """Return a new random RFC 9562 UUID version 7."""
    unix_ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFFFFFFFFFFFFFF
    value = (unix_ts_ms << 80) | (_VERSION_7 << 76) | (rand_a << 64) | (_VARIANT_10 << 62) | rand_b
    return uuid.UUID(int=value)
