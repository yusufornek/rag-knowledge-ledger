"""Shared fixtures for `tests/cli/`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_SNAPSHOTS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "snapshots"


@pytest.fixture
def corpus_dir() -> Path:
    """The committed synthetic document corpus under `tests/fixtures/corpus/`."""
    return _CORPUS_DIR


@pytest.fixture
def snapshots_dir() -> Path:
    """Committed `.ndjson.zst` snapshot fixtures under `tests/fixtures/snapshots/`."""
    return _SNAPSHOTS_DIR


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


_MINIMAL_CONFIG = """\
version: 1
namespace: {namespace}
sources:
  root: {root}
embedding:
  mode: deterministic
  dimension: 8
governance:
  pii: true
  license_default: NOASSERTION
  acl_required: false
  tenant_required: false
manifest:
  reproducible: true
"""


def _write_minimal_config(
    path: Path, *, root: Path, namespace: str = "cli-test", extra: str = ""
) -> Path:
    """Write a minimal, valid ragledger.yml at ``path`` and return it.

    Uses `embedding.mode: deterministic` so builds run with no
    `model-revisions.lock` requirement and no network access, matching
    this project's "no network in tests" constraint.
    """
    path.write_text(
        _MINIMAL_CONFIG.format(namespace=namespace, root=root.as_posix()) + extra, encoding="utf-8"
    )
    return path


@pytest.fixture
def write_minimal_config() -> Callable[..., Path]:
    """Factory fixture: `write_minimal_config(path, root=..., namespace=..., extra=...)`."""
    return _write_minimal_config
