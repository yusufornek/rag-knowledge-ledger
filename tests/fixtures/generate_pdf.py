"""Generates `tests/fixtures/corpus/sample.pdf`.

A hand-built, minimal, valid single-page PDF (Helvetica text via `Tj`
operators in an uncompressed content stream) -- no `reportlab` or
similar dependency needed. Run once to (re)produce the committed
fixture; not a test module itself (no `test_` prefix, so pytest does
not collect it) and not imported by any test.

Deliberately kept a level above `tests/fixtures/corpus/` rather than
inside it: `corpus/` is fed directly to `ragledger.pipeline.discovery`
in pipeline integration tests, and this script is test infrastructure,
not a document fixture.

Usage: ``uv run python tests/fixtures/generate_pdf.py``
"""

from __future__ import annotations

from pathlib import Path

_LINES = [
    "RAG Knowledge Ledger sample PDF fixture.",
    "This file exists to exercise the pypdf-backed PDF parser adapter.",
    "Synthetic canary contact: pdf.canary.leaktest@example.com",
    "The address above is fictional and used only for PII detector tests.",
    "A synthetic payment sandbox card also appears here: 4111 1111 1111 1111.",
]


def _escape_pdf_string(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_minimal_pdf(lines: list[str]) -> bytes:
    objects: list[bytes] = []

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    stream_parts = [b"BT /F1 12 Tf 72 720 Td"]
    for index, line in enumerate(lines):
        prefix = b"" if index == 0 else b" 0 -16 Td"
        stream_parts.append(prefix + b" (" + _escape_pdf_string(line).encode("latin-1") + b") Tj")
    stream_parts.append(b" ET")
    content_stream = b"\n".join(stream_parts)
    objects.append(
        b"<< /Length "
        + str(len(content_stream)).encode("ascii")
        + b" >>\nstream\n"
        + content_stream
        + b"\nendstream"
    )

    header = b"%PDF-1.4\n"
    body_parts: list[bytes] = []
    offsets: list[int] = []
    cursor = len(header)
    for index, obj_body in enumerate(objects, start=1):
        offsets.append(cursor)
        obj_bytes = f"{index} 0 obj\n".encode("ascii") + obj_body + b"\nendobj\n"
        body_parts.append(obj_bytes)
        cursor += len(obj_bytes)

    xref_offset = cursor
    xref_lines = [b"xref", f"0 {len(objects) + 1}".encode("ascii"), b"0000000000 65535 f "]
    for offset in offsets:
        xref_lines.append(f"{offset:010d} 00000 n ".encode("ascii"))
    xref_section = b"\n".join(xref_lines) + b"\n"

    trailer_text = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF"
    )
    trailer = trailer_text.encode("ascii")

    return header + b"".join(body_parts) + xref_section + trailer


def main() -> None:
    output_path = Path(__file__).resolve().parent / "corpus" / "sample.pdf"
    output_path.write_bytes(build_minimal_pdf(_LINES))


if __name__ == "__main__":
    main()
