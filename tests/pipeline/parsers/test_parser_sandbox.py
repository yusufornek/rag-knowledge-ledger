"""Tests for `ragledger.pipeline.parsers.sandbox`: isolation of untrusted parsing.

Covers PROJECT_SPEC.md section 8.3's sandbox requirement: a
crashing/hanging/oversized-output parser must yield a `ParseOutcome`
with `status="fail"`, never a raised exception or a hung/crashed test
process. `tests/pipeline/parsers/_malicious_stub_parsers.py` provides
the misbehaving stub parsers this file drives through the real sandbox
subprocess boundary.
"""

from __future__ import annotations

import time
from pathlib import Path

from ragledger.pipeline.parsers.base import ParseLimits
from ragledger.pipeline.parsers.pdf import PdfParser
from ragledger.pipeline.parsers.sandbox import parser_ref_for, run_sandboxed
from ragledger.pipeline.parsers.text import PlainTextParser

_STUBS_PATH = Path(__file__).resolve().parent / "_malicious_stub_parsers.py"


def _load_stub_class(class_name: str):  # type: ignore[no-untyped-def]
    import importlib.util
    import sys

    module_name = "malicious_stub_parsers_direct"
    spec = importlib.util.spec_from_file_location(module_name, _STUBS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registering in sys.modules before exec_module matters: inspect.getfile
    # (which ragledger.pipeline.parsers.sandbox.parser_ref_for relies on)
    # looks the class's module up by name in sys.modules, not just by the
    # module object's own __file__ attribute.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return getattr(module, class_name)


def test_parser_ref_for_locates_a_real_parser_by_file_path() -> None:
    ref = parser_ref_for(PlainTextParser())
    assert ref.class_name == "PlainTextParser"
    assert Path(ref.module_file).is_file()
    assert Path(ref.module_file).name == "text.py"


def test_sandboxed_well_behaved_parser_returns_normal_result() -> None:
    outcome = run_sandboxed(
        PlainTextParser(), b"Hello world.\n\nSecond paragraph.", {}, ParseLimits()
    )
    assert outcome.status == "success"
    assert outcome.document is not None
    assert len(outcome.document.elements) == 2


def test_sandboxed_infinite_loop_parser_times_out_instead_of_hanging() -> None:
    stub = _load_stub_class("InfiniteLoopParser")()
    start = time.monotonic()
    outcome = run_sandboxed(stub, b"x", {}, ParseLimits(timeout_seconds=1.0))
    elapsed = time.monotonic() - start
    assert outcome.status == "fail"
    assert outcome.errors[0].startswith("PARSE_TIMEOUT")
    # the call returns close to the configured timeout, not hanging indefinitely
    assert elapsed < 5.0


def test_sandboxed_oversized_output_parser_fails_instead_of_returning_huge_payload() -> None:
    stub = _load_stub_class("OversizedOutputParser")()
    outcome = run_sandboxed(stub, b"x", {}, ParseLimits(max_output_bytes=4096))
    assert outcome.status == "fail"
    assert outcome.errors[0].startswith("PARSE_OUTPUT_TOO_LARGE")
    assert outcome.document is None


def test_sandboxed_crashing_parser_reports_failure_not_an_exception() -> None:
    stub = _load_stub_class("CrashingParser")()
    outcome = run_sandboxed(stub, b"x", {}, ParseLimits())
    assert outcome.status == "fail"
    assert "PARSE_EXCEPTION" in outcome.errors[0] or "PARSE_CRASHED" in outcome.errors[0]


def test_sandboxed_hard_process_exit_reports_failure_not_a_pipeline_crash() -> None:
    stub = _load_stub_class("ProcessExitParser")()
    outcome = run_sandboxed(stub, b"x", {}, ParseLimits())
    assert outcome.status == "fail"
    assert outcome.errors[0].startswith("PARSE_CRASHED")


def test_sandboxed_broken_pdf_fails_cleanly_through_the_subprocess_boundary() -> None:
    outcome = run_sandboxed(PdfParser(), b"not a pdf file", {}, ParseLimits())
    assert outcome.status == "fail"
    assert outcome.errors[0].startswith("PDF_PARSE_ERROR")


def test_sandbox_isolation_does_not_leave_stray_output_files() -> None:
    # A crash/timeout must not leave partial state visible to the caller:
    # run_sandboxed always uses a fresh TemporaryDirectory that is cleaned
    # up regardless of outcome, even when the sandboxed parser crashes.
    import tempfile

    temp_root = Path(tempfile.gettempdir())
    stub = _load_stub_class("CrashingParser")()
    before = set(temp_root.glob("ragledger-sandbox-*"))
    run_sandboxed(stub, b"x", {}, ParseLimits())
    after = set(temp_root.glob("ragledger-sandbox-*"))
    assert after - before == set()
