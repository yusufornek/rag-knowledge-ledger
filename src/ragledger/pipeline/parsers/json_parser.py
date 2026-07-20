"""Native deterministic parser for `application/json` sources (FR-020).

`json.loads` is standard library, deterministic, and never touches the
network or executes anything in the document. Structure is mapped onto
`LedgerElement`s as follows, so a JSON source still gets meaningful
chunk-sized units rather than becoming one opaque blob:

- A top-level array of objects/values: one paragraph element per item,
  rendered as canonical (key-sorted) JSON text.
- A top-level object: one paragraph element per key, rendered as
  ``"<key>: <value JSON>"``.
- A top-level scalar: a single paragraph element.

Rendering always uses ``sort_keys=True`` so the produced text -- and
therefore every downstream hash -- depends only on the JSON value, never
on the byte order of keys in the original source file.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from ragledger.core.hashing import hash_raw_bytes
from ragledger.pipeline.parsers.base import (
    LedgerDocument,
    LedgerElement,
    ParseLimits,
    ParseOutcome,
    ParserDescriptor,
)

_PARSER_NAME = "ragledger.native_json"
_PARSER_VERSION = "1"
_ALLOWED_CONFIG_KEYS = {"encoding"}


def _render(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


class JsonParser:
    """Deterministic native parser for `application/json`."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name=_PARSER_NAME, version=_PARSER_VERSION, package_distributions=[]
        )

    def supports(self, media_type: str) -> bool:
        return media_type == "application/json"

    def validate_config(self, config: Mapping[str, Any]) -> None:
        unknown = set(config) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown json parser config keys: {sorted(unknown)}")

    def parse(self, data: bytes, config: Mapping[str, Any], limits: ParseLimits) -> ParseOutcome:
        start = time.monotonic()
        consumed_hash = hash_raw_bytes(data)
        if len(data) > limits.max_input_bytes:
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=["INPUT_TOO_LARGE"],
                duration_seconds=time.monotonic() - start,
            )
        encoding = config.get("encoding", "utf-8")
        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=[f"DECODE_ERROR: {exc}"],
                duration_seconds=time.monotonic() - start,
            )
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=[f"JSON_DECODE_ERROR: {exc}"],
                duration_seconds=time.monotonic() - start,
            )
        elements: list[LedgerElement] = []
        if isinstance(value, list):
            for index, item in enumerate(value):
                elements.append(
                    LedgerElement(
                        id=f"e{index}",
                        kind="paragraph",
                        order=index,
                        text=_render(item),
                        heading_path=[f"item {index}"],
                    )
                )
        elif isinstance(value, dict):
            for index, key in enumerate(sorted(value)):
                elements.append(
                    LedgerElement(
                        id=f"e{index}",
                        kind="paragraph",
                        order=index,
                        text=f"{key}: {_render(value[key])}",
                        heading_path=[key],
                    )
                )
        else:
            elements.append(LedgerElement(id="e0", kind="paragraph", order=0, text=_render(value)))
        document = LedgerDocument(elements=elements, page_count=1)
        return ParseOutcome(
            status="success",
            document=document,
            consumed_input_hash=consumed_hash,
            duration_seconds=time.monotonic() - start,
        )
