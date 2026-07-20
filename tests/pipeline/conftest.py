"""Shared fixtures for `tests/pipeline/`."""

from __future__ import annotations

from pathlib import Path

import pytest

_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"


@pytest.fixture
def corpus_dir() -> Path:
    """The committed synthetic document corpus under `tests/fixtures/corpus/`."""
    return _CORPUS_DIR
