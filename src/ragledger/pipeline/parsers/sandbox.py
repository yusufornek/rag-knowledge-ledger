"""Untrusted parser execution sandbox (PROJECT_SPEC.md section 8.3, 34.6).

Runs a `DocumentParser.parse` call in a separate subprocess, so that a
parser which crashes, hangs, or tries to allocate unbounded memory or
output never brings down the process running the build. The controller
here degrades every such failure mode into an ordinary `fail`
`ParseOutcome` -- the same shape a well-behaved parser returns for input
it cannot handle -- so callers never need to special-case "the parser
misbehaved" versus "the parser reported a clean failure".

Isolation mechanisms, in order of what actually stops each threat:

- **Hang / infinite loop**: `subprocess.run(..., timeout=...)`. A
  timeout kills the child process tree; the controller reports
  `PARSE_TIMEOUT`.
- **Oversized output ("zip-bomb-like" growth)**: the worker itself
  refuses to write a result larger than `ParseLimits.max_output_bytes`
  (substituting a failure payload instead), and the controller
  independently re-checks the written file's size before reading it, so
  a compromised or buggy worker cannot bypass the cap by writing the
  cap-violating bytes anyway.
- **Crash / unhandled exception / non-zero exit**: the worker catches
  exceptions raised by the parser itself and reports them as a `fail`
  outcome; if the *worker process itself* dies (segfault, `SystemExit`,
  killed), the controller notices the non-zero return code or missing
  output file and reports `PARSE_CRASHED`.
- **Excess memory**: best-effort `RLIMIT_AS` on platforms that support
  it (see `_memory_preexec_fn`); not relied upon as the sole defense,
  since it is not portable (for example, unavailable on Windows).

A parser class is located across the process boundary by absolute file
path plus class name (`ParserRef`), not by dotted module import: this
lets test-only stub parsers (used to exercise the "hangs" and "produces
oversized output" failure modes) live anywhere on disk, including
outside any installed or `sys.path`-visible package, without needing
`tests/` to be an importable package itself.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ragledger.core.hashing import hash_raw_bytes
from ragledger.pipeline.parsers.base import DocumentParser, ParseLimits, ParseOutcome

_WORKER_MODULE = "ragledger.pipeline.parsers._sandbox_worker"


@dataclass(frozen=True)
class ParserRef:
    """Locates a parser class by file path, for cross-process re-loading."""

    module_file: str
    class_name: str


def parser_ref_for(parser: DocumentParser) -> ParserRef:
    cls = type(parser)
    return ParserRef(module_file=str(Path(inspect.getfile(cls)).resolve()), class_name=cls.__name__)


def _memory_preexec_fn(max_memory_bytes: int) -> Any:
    """Return a `preexec_fn` applying a best-effort `RLIMIT_AS` cap, or `None`.

    Only usable on POSIX; `subprocess.Popen(preexec_fn=...)` is itself
    POSIX-only. Failures to apply the limit are swallowed inside the
    child (it simply runs unconstrained) rather than raised, since this
    is defense in depth on top of the timeout and output-size checks,
    not the only thing standing between a hostile parser and the host.
    """
    if sys.platform == "win32":
        return None
    try:
        import resource
    except ImportError:  # pragma: no cover - resource is POSIX-only
        return None

    def _apply() -> None:
        with contextlib.suppress(Exception, ValueError):
            resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))

    return _apply


def _failure_outcome(data: bytes, code: str, message: str, duration: float) -> ParseOutcome:
    return ParseOutcome(
        status="fail",
        consumed_input_hash=hash_raw_bytes(data),
        errors=[f"{code}: {message}"],
        duration_seconds=duration,
    )


def run_sandboxed(
    parser: DocumentParser,
    data: bytes,
    config: Mapping[str, Any],
    limits: ParseLimits,
) -> ParseOutcome:
    """Run ``parser.parse(data, config, limits)`` inside a sandboxed subprocess.

    Always returns a `ParseOutcome`; never raises for a misbehaving
    parser (timeout, crash, oversized output all become `status="fail"`
    outcomes with a stable error code).
    """
    ref = parser_ref_for(parser)
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ragledger-sandbox-") as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / "input.bin"
        config_path = tmp_path / "config.json"
        output_path = tmp_path / "output.json"
        spec_path = tmp_path / "spec.json"

        input_path.write_bytes(data)
        config_path.write_text(json.dumps(dict(config)), encoding="utf-8")
        spec_path.write_text(
            json.dumps(
                {
                    "module_file": ref.module_file,
                    "class_name": ref.class_name,
                    "input_path": str(input_path),
                    "config_path": str(config_path),
                    "output_path": str(output_path),
                    "limits": {
                        "max_input_bytes": limits.max_input_bytes,
                        "max_pages": limits.max_pages,
                        "max_output_bytes": limits.max_output_bytes,
                        "timeout_seconds": limits.timeout_seconds,
                        "max_memory_bytes": limits.max_memory_bytes,
                    },
                }
            ),
            encoding="utf-8",
        )

        preexec_fn = (
            _memory_preexec_fn(limits.max_memory_bytes) if limits.max_memory_bytes else None
        )
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted interpreter
                [sys.executable, "-m", _WORKER_MODULE, str(spec_path)],
                timeout=limits.timeout_seconds,
                capture_output=True,
                preexec_fn=preexec_fn,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _failure_outcome(
                data,
                "PARSE_TIMEOUT",
                "parser exceeded the sandbox time limit",
                time.monotonic() - start,
            )

        duration = time.monotonic() - start
        if result.returncode != 0 or not output_path.exists():
            detail = result.stderr.decode("utf-8", errors="replace")[-2000:]
            return _failure_outcome(
                data,
                "PARSE_CRASHED",
                f"sandbox worker exited with code {result.returncode}: {detail}",
                duration,
            )
        if output_path.stat().st_size > limits.max_output_bytes:
            return _failure_outcome(
                data,
                "PARSE_OUTPUT_TOO_LARGE",
                "parser output exceeded the sandbox size cap",
                duration,
            )
        payload = json.loads(output_path.read_bytes())
        return ParseOutcome.model_validate(payload)
