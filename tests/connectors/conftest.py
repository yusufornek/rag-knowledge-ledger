"""Shared pytest configuration for `tests/connectors/`.

Registers the `integration` marker `tests/connectors/test_integration.py`
uses, so pytest does not warn about an unregistered marker. This is
the appropriate place for that registration: `pyproject.toml`'s
`[tool.pytest.ini_options]` table is owned by concurrent work on this
milestone, not this package's test suite.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires live Qdrant/Postgres from docker-compose.yml; "
        "skipped unless the RAGLEDGER_IT=1 environment variable is set.",
    )
