"""Native deterministic parser for `text/markdown` sources (FR-020).

A line-oriented state machine, not a full CommonMark implementation:
headings (`#`..`######`), fenced code blocks, list items, simple pipe
tables (with an optional `Table: <caption>` line immediately before the
table recognized as its caption), and blank-line-separated paragraphs.
Optional leading YAML frontmatter (`---` ... `---`) is parsed with
`yaml.safe_load` and exposed as `LedgerDocument.frontmatter` rather than
folded into body elements, so `ragledger.governance.license` can read a
declared `license:` field without re-parsing the source.

No network access, no macro/embedded-file execution: this parser only
ever reads the bytes it is given.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

import yaml

from ragledger.core.hashing import hash_raw_bytes
from ragledger.core.models import WarningRecord
from ragledger.pipeline.parsers.base import (
    LedgerDocument,
    LedgerElement,
    ParseLimits,
    ParseOutcome,
    ParserDescriptor,
)

_PARSER_NAME = "ragledger.native_markdown"
_PARSER_VERSION = "1"
_ALLOWED_CONFIG_KEYS = {"encoding"}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")
_FENCE_RE = re.compile(r"^(```|~~~)(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_CAPTION_RE = re.compile(r"^Table:\s*(.+)$", re.IGNORECASE)
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?\r?\n)---\r?\n?", re.DOTALL)


class MarkdownParser:
    """Deterministic native parser for `text/markdown`."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name=_PARSER_NAME, version=_PARSER_VERSION, package_distributions=[]
        )

    def supports(self, media_type: str) -> bool:
        return media_type == "text/markdown"

    def validate_config(self, config: Mapping[str, Any]) -> None:
        unknown = set(config) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown markdown parser config keys: {sorted(unknown)}")

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
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        warnings: list[WarningRecord] = []
        frontmatter, body = _extract_frontmatter(normalized, warnings)
        elements = _parse_body(body)
        document = LedgerDocument(elements=elements, page_count=1, frontmatter=frontmatter)
        return ParseOutcome(
            status="success",
            document=document,
            warnings=warnings,
            consumed_input_hash=consumed_hash,
            duration_seconds=time.monotonic() - start,
        )


def _extract_frontmatter(
    text: str, warnings: list[WarningRecord]
) -> tuple[dict[str, Any] | None, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    raw_yaml = match.group(1)
    try:
        loaded = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        warnings.append(
            WarningRecord(code="FRONTMATTER_YAML_INVALID", message="frontmatter is not valid YAML")
        )
        return None, text[match.end() :]
    if not isinstance(loaded, dict):
        warnings.append(
            WarningRecord(
                code="FRONTMATTER_NOT_A_MAPPING",
                message="frontmatter did not parse to a YAML mapping",
            )
        )
        return None, text[match.end() :]
    return loaded, text[match.end() :]


def _parse_body(body: str) -> list[LedgerElement]:  # noqa: C901 - line-oriented state machine
    elements: list[LedgerElement] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph_buffer: list[str] = []
    table_rows: list[list[str]] = []
    order = 0

    def heading_path() -> list[str]:
        return [text for _, text in heading_stack]

    def next_id() -> str:
        nonlocal order
        element_id = f"e{order}"
        order += 1
        return element_id

    def flush_paragraph() -> None:
        if not paragraph_buffer:
            return
        joined = " ".join(line.strip() for line in paragraph_buffer if line.strip())
        paragraph_buffer.clear()
        if not joined:
            return
        elements.append(
            LedgerElement(
                id=next_id(), kind="paragraph", order=0, text=joined, heading_path=heading_path()
            )
        )

    def flush_table() -> None:
        if not table_rows:
            return
        caption = None
        if elements and elements[-1].kind == "paragraph":
            match = _CAPTION_RE.match(elements[-1].text)
            if match:
                caption = match.group(1).strip()
                elements.pop()
        header_text = " | ".join(cell.strip() for cell in table_rows[0])
        for row in table_rows:
            row_text = " | ".join(cell.strip() for cell in row)
            elements.append(
                LedgerElement(
                    id=next_id(),
                    kind="table",
                    order=0,
                    text=row_text,
                    heading_path=heading_path(),
                    table_caption=caption,
                    table_header=header_text,
                )
            )
        table_rows.clear()

    lines = body.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]

        fence_match = _FENCE_RE.match(line.strip())
        if fence_match:
            flush_paragraph()
            flush_table()
            fence_marker = fence_match.group(1)
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(fence_marker):
                code_lines.append(lines[index])
                index += 1
            index += 1  # skip closing fence (or move past end of input)
            elements.append(
                LedgerElement(
                    id=next_id(),
                    kind="code",
                    order=0,
                    text="\n".join(code_lines),
                    heading_path=heading_path(),
                )
            )
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush_paragraph()
            flush_table()
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip()
            heading_stack[:] = [item for item in heading_stack if item[0] < level]
            elements.append(
                LedgerElement(
                    id=next_id(),
                    kind="title" if level == 1 else "heading",
                    order=0,
                    text=heading_text,
                    heading_path=heading_path(),
                )
            )
            heading_stack.append((level, heading_text))
            index += 1
            continue

        if _TABLE_ROW_RE.match(line):
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            if not table_rows and _TABLE_SEPARATOR_RE.match(next_line) and "-" in next_line:
                flush_paragraph()
                header_cells = list(line.strip().strip("|").split("|"))
                table_rows.append(header_cells)
                index += 2  # header line + separator line
                continue
            if table_rows:
                row_cells = list(line.strip().strip("|").split("|"))
                table_rows.append(row_cells)
                index += 1
                continue

        if table_rows:
            flush_table()

        list_match = _LIST_ITEM_RE.match(line)
        if list_match:
            flush_paragraph()
            item_text = list_match.group(3).strip()
            elements.append(
                LedgerElement(
                    id=next_id(),
                    kind="list_item",
                    order=0,
                    text=item_text,
                    heading_path=heading_path(),
                )
            )
            index += 1
            continue

        if not line.strip():
            flush_paragraph()
            index += 1
            continue

        paragraph_buffer.append(line)
        index += 1

    flush_paragraph()
    flush_table()

    for position, element in enumerate(elements):
        elements[position] = element.model_copy(update={"order": position})
    return elements
