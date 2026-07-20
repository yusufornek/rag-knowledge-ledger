"""Native deterministic parser for `text/html` sources (FR-020).

Built on the standard library's `html.parser.HTMLParser` -- a real
tokenizing HTML parser, not a regular-expression scrape -- so malformed
markup is handled the same way a browser's tokenizer would degrade,
rather than producing silently wrong matches. Headings (`h1`..`h6`),
paragraphs, list items, `pre` code blocks, and `table`/`caption` are
mapped onto `LedgerElement`; `script`/`style` content is never emitted
as text (never executed either -- this parser only reads).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from ragledger.core.hashing import hash_raw_bytes
from ragledger.pipeline.parsers.base import (
    ElementKind,
    LedgerDocument,
    LedgerElement,
    ParseLimits,
    ParseOutcome,
    ParserDescriptor,
)

_PARSER_NAME = "ragledger.native_html"
_PARSER_VERSION = "1"
"""No backing third-party distribution: this parser wraps the standard
library's bundled `html.parser`, whose behavior is tied to the Python
runtime rather than an independently released package version."""

_ALLOWED_CONFIG_KEYS = {"encoding"}
_HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}
_SKIP_CONTENT_TAGS = {"script", "style"}
_SIMPLE_CAPTURE_TAGS = {"p", "li", "pre"}


class _TableState:
    def __init__(self) -> None:
        self.rows: list[list[list[str]]] = []
        self.in_caption = False
        self.caption_parts: list[str] = []


class _CaptureFrame:
    def __init__(self, kind: ElementKind, level: int | None = None) -> None:
        self.kind = kind
        self.level = level
        self.parts: list[str] = []


class _Builder(HTMLParser):
    """Accumulates `LedgerElement` records while walking the token stream."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[LedgerElement] = []
        self._order = 0
        self._heading_stack: list[tuple[int, str]] = []
        self._skip_depth = 0
        self._paragraph_buffer: list[str] = []
        self._capture_stack: list[_CaptureFrame] = []
        self._table_stack: list[_TableState] = []

    def _heading_path(self) -> list[str]:
        return [text for _, text in self._heading_stack]

    def _next_id(self) -> str:
        element_id = f"e{self._order}"
        self._order += 1
        return element_id

    def _flush_implicit_paragraph(self) -> None:
        text = " ".join(part.strip() for part in self._paragraph_buffer if part.strip())
        self._paragraph_buffer.clear()
        if text:
            self.elements.append(
                LedgerElement(
                    id=self._next_id(),
                    kind="paragraph",
                    order=0,
                    text=text,
                    heading_path=self._heading_path(),
                )
            )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._table_stack:
            state = self._table_stack[-1]
            if tag == "caption":
                state.in_caption = True
            elif tag == "tr":
                state.rows.append([])
            elif tag in ("td", "th"):
                if not state.rows:
                    state.rows.append([])
                state.rows[-1].append([])
            return
        if tag in _HEADING_TAGS:
            self._flush_implicit_paragraph()
            self._capture_stack.append(_CaptureFrame("heading", level=_HEADING_TAGS[tag]))
            return
        if tag == "table":
            self._flush_implicit_paragraph()
            self._table_stack.append(_TableState())
            return
        if tag in _SIMPLE_CAPTURE_TAGS:
            self._flush_implicit_paragraph()
            kind: ElementKind = (
                "list_item" if tag == "li" else ("code" if tag == "pre" else "paragraph")
            )
            self._capture_stack.append(_CaptureFrame(kind))
            return

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._table_stack and tag == "table":
            state = self._table_stack.pop()
            self._emit_table(state)
            return
        if self._table_stack:
            if tag == "caption":
                self._table_stack[-1].in_caption = False
            return
        if (
            tag in _HEADING_TAGS
            and self._capture_stack
            and self._capture_stack[-1].kind == "heading"
        ):
            frame = self._capture_stack.pop()
            text = " ".join(part.strip() for part in frame.parts if part.strip())
            level = frame.level or 1
            if text:
                kind: ElementKind = "title" if level == 1 else "heading"
                self.elements.append(
                    LedgerElement(
                        id=self._next_id(),
                        kind=kind,
                        order=0,
                        text=text,
                        heading_path=self._heading_path(),
                    )
                )
                self._heading_stack[:] = [item for item in self._heading_stack if item[0] < level]
                self._heading_stack.append((level, text))
            return
        if tag in _SIMPLE_CAPTURE_TAGS and self._capture_stack:
            frame = self._capture_stack.pop()
            if frame.kind == "code":
                text = "".join(frame.parts).strip("\n")
            else:
                text = " ".join(part.strip() for part in frame.parts if part.strip())
            if text:
                self.elements.append(
                    LedgerElement(
                        id=self._next_id(),
                        kind=frame.kind,
                        order=0,
                        text=text,
                        heading_path=self._heading_path(),
                    )
                )
            return

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._table_stack:
            state = self._table_stack[-1]
            if state.in_caption:
                state.caption_parts.append(data)
            elif state.rows and state.rows[-1]:
                state.rows[-1][-1].append(data)
            return
        if self._capture_stack:
            self._capture_stack[-1].parts.append(data)
            return
        if data.strip():
            self._paragraph_buffer.append(data)

    def _emit_table(self, state: _TableState) -> None:
        cell_rows = [
            ["".join(cell).strip() for cell in row]
            for row in state.rows
            if any("".join(cell).strip() for cell in row)
        ]
        if not cell_rows:
            return
        caption = " ".join(part.strip() for part in state.caption_parts if part.strip())
        header_text = " | ".join(cell_rows[0])
        for row in cell_rows:
            self.elements.append(
                LedgerElement(
                    id=self._next_id(),
                    kind="table",
                    order=0,
                    text=" | ".join(row),
                    heading_path=self._heading_path(),
                    table_caption=caption or None,
                    table_header=header_text,
                )
            )

    def finalize(self) -> list[LedgerElement]:
        self._flush_implicit_paragraph()
        for position, element in enumerate(self.elements):
            self.elements[position] = element.model_copy(update={"order": position})
        return self.elements


class HtmlDocumentParser:
    """Deterministic native parser for `text/html`."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name=_PARSER_NAME, version=_PARSER_VERSION, package_distributions=[]
        )

    def supports(self, media_type: str) -> bool:
        return media_type == "text/html"

    def validate_config(self, config: Mapping[str, Any]) -> None:
        unknown = set(config) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown html parser config keys: {sorted(unknown)}")

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
        builder = _Builder()
        try:
            builder.feed(text)
            builder.close()
        except Exception as exc:  # html.parser is lenient; guard defensively regardless
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=[f"HTML_PARSE_ERROR: {type(exc).__name__}: {exc}"],
                duration_seconds=time.monotonic() - start,
            )
        document = LedgerDocument(elements=builder.finalize(), page_count=1)
        return ParseOutcome(
            status="success",
            document=document,
            consumed_input_hash=consumed_hash,
            duration_seconds=time.monotonic() - start,
        )
