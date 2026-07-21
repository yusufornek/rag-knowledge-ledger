"""Minimal foundation tests for the ragledger package.

These tests verify the package is importable, exposes the expected
version, that the CLI entry point runs, and that the design-time JSON
Schema documents under docs/spec/ are themselves well-formed JSON Schema
draft 2020-12 documents.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path

import jsonschema
import pytest
from jsonschema.validators import Draft202012Validator
from typer.testing import CliRunner

import ragledger
from ragledger.cli import app

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "docs" / "spec"


def test_package_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", ragledger.__version__)
    assert ragledger.__version__ == importlib.metadata.version("ragledger")


def test_package_exports_version_in_all() -> None:
    assert "__version__" in ragledger.__all__


def test_cli_version_command_prints_package_version() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == ragledger.__version__


@pytest.mark.parametrize(
    "schema_filename",
    [
        "manifest-v1.schema.json",
        "policy-v1.schema.json",
    ],
)
def test_schema_file_is_valid_json_schema_2020_12(schema_filename: str) -> None:
    schema_path = SCHEMA_DIR / schema_filename
    with schema_path.open(encoding="utf-8") as handle:
        schema = json.load(handle)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    # Raises jsonschema.exceptions.SchemaError if the document is not a
    # valid draft 2020-12 schema.
    Draft202012Validator.check_schema(schema)


def test_manifest_schema_rejects_empty_object() -> None:
    schema_path = SCHEMA_DIR / "manifest-v1.schema.json"
    with schema_path.open(encoding="utf-8") as handle:
        schema = json.load(handle)

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance={}, schema=schema)


def test_policy_schema_rejects_unknown_top_level_key() -> None:
    schema_path = SCHEMA_DIR / "policy-v1.schema.json"
    with schema_path.open(encoding="utf-8") as handle:
        schema = json.load(handle)

    instance = {
        "version": 1,
        "name": "example",
        "requirements": {},
        "findings": {"fail_on_severity": ["critical"]},
        "unexpected_field": True,
    }

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=instance, schema=schema)
