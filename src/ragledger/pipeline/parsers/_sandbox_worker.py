"""Sandbox subprocess entry point.

Invoked as ``python -m ragledger.pipeline.parsers._sandbox_worker <spec_path>``
by `ragledger.pipeline.parsers.sandbox.run_sandboxed`. This module is a
process boundary, not a public API: application code never imports it
directly.

The parser class is loaded from an absolute file path (not a dotted
import) so that test-only stub parsers used to exercise sandbox failure
modes can live anywhere on disk. Any exception the parser itself raises
(including during class loading) is caught here and turned into a
`fail` `ParseOutcome` written to the output file with exit code 0 --
that is a parser reporting a failure cleanly, which is different from
the worker process itself crashing (nonzero exit, no output file),
which the controller in `sandbox.py` detects independently.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ragledger.core.hashing import hash_raw_bytes
from ragledger.pipeline.parsers.base import ParseLimits, ParseOutcome


def _load_parser_class(module_file: str, class_name: str) -> Any:
    module_name = f"_ragledger_sandbox_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load parser module from {module_file!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return getattr(module, class_name)


def _failure_payload(data: bytes, code: str, message: str, duration: float) -> dict[str, Any]:
    outcome = ParseOutcome(
        status="fail",
        consumed_input_hash=hash_raw_bytes(data),
        errors=[f"{code}: {message}"],
        duration_seconds=duration,
    )
    return outcome.model_dump(mode="json", exclude_none=True)


def main(argv: list[str]) -> int:
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    input_path = Path(spec["input_path"])
    output_path = Path(spec["output_path"])
    data = input_path.read_bytes()
    limits = ParseLimits(**spec["limits"])
    config = json.loads(Path(spec["config_path"]).read_text(encoding="utf-8"))

    start = time.monotonic()
    try:
        parser_cls = _load_parser_class(spec["module_file"], spec["class_name"])
        parser = parser_cls()
        outcome = parser.parse(data, config, limits)
        payload = outcome.model_dump(mode="json", exclude_none=True)
    except Exception as exc:  # the parser (or loading it) misbehaved; report, never propagate
        payload = _failure_payload(
            data, "PARSE_EXCEPTION", f"{type(exc).__name__}: {exc}", time.monotonic() - start
        )

    encoded = json.dumps(payload).encode("utf-8")
    if len(encoded) > limits.max_output_bytes:
        payload = _failure_payload(
            data,
            "PARSE_OUTPUT_TOO_LARGE",
            f"serialized output was {len(encoded)} bytes, cap is {limits.max_output_bytes}",
            time.monotonic() - start,
        )
        encoded = json.dumps(payload).encode("utf-8")
    output_path.write_bytes(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
