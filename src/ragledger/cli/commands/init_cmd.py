"""`ragledger init`, per the design specification section 17.1 and 17.3.

Writes a `ragledger.yml` config skeleton and a `.ragledgerignore`
example into a target directory (default: cwd). Never overwrites an
existing file without `--force`.

The generated `embedding:` block defaults to `mode: deterministic`
rather than section 17.3's literal `mode: local` example, so
`ragledger init && ragledger build .` works immediately with no model
download, no network access, and no `model-revisions.lock` to author
first. The commented-out alternative in the generated file shows the
literal spec shape (`mode: local` + `revision_file`) for operators who
want to declare a real model identity; see
`ragledger.cli._build_support` for what that mode actually does in this
release.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ragledger.cli._exit import EXIT_CONFIG_ERROR, CliError, run_command
from ragledger.cli._output import log

_CONFIG_TEMPLATE = """\
# ragledger.yml -- RAG Knowledge Ledger build configuration.
# See the design specification section 17.3 for the full field reference.
version: 1
namespace: {namespace}

sources:
  root: ./documents
  include:
    - "**/*.pdf"
    - "**/*.docx"
    - "**/*.md"
    - "**/*.txt"

parser:
  name: docling
  ocr:
    enabled: false
    languages: [eng]

chunker:
  strategy: hybrid
  max_tokens: 700
  overlap_tokens: 100
  tokenizer: sentence-transformers/all-MiniLM-L6-v2

# mode: deterministic uses ragledger's built-in, network-free reference
# embedder (ragledger.pipeline.embedding.DeterministicLocalEmbeddingProvider).
# Its vectors carry stable content identity but no learned semantic
# meaning -- suitable for building/inspecting a manifest immediately,
# not for real retrieval quality.
#
# To declare a real local model instead, use the design specification section
# 17.3 shape:
#   embedding:
#     mode: local
#     model: sentence-transformers/all-MiniLM-L6-v2
#     revision_file: ./model-revisions.lock
#     normalize: true
# `revision_file` must then pin the model to an immutable commit SHA
# (never a mutable alias like "main"). Real sentence-transformers
# inference is not wired in this release;
# `ragledger build` still substitutes the deterministic reference
# embedder in `local` mode and says so in its output.
embedding:
  mode: deterministic
  model: sentence-transformers/all-MiniLM-L6-v2
  dimension: 32
  normalize: true

governance:
  pii: true
  license_default: NOASSERTION
  acl_required: false
  tenant_required: false

manifest:
  reproducible: true
"""

_IGNORE_TEMPLATE = """\
# .ragledgerignore -- gitignore-syntax patterns excluded from discovery.
.git/
.ragledger/
*.tmp
*.log
"""


def init(
    directory: Path = typer.Argument(  # noqa: B008
        Path("."), help="Directory to initialize (default: current directory)."
    ),
    namespace: str = typer.Option(
        "default", "--namespace", help="Namespace to write into ragledger.yml."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing ragledger.yml/.ragledgerignore."
    ),
) -> None:
    """Create a ragledger.yml config skeleton and a .ragledgerignore example."""
    run_command(lambda: _init_impl(directory, namespace, force))


def _init_impl(directory: Path, namespace: str, force: bool) -> None:
    target_dir = directory.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    config_path = target_dir / "ragledger.yml"
    ignore_path = target_dir / ".ragledgerignore"

    for existing in (config_path, ignore_path):
        if existing.exists() and not force:
            raise CliError(
                f"{existing} already exists; pass --force to overwrite",
                exit_code=EXIT_CONFIG_ERROR,
            )

    # `json.dumps` produces a double-quoted, escaped string that is also
    # valid YAML, so an operator-supplied namespace can never break the
    # generated document's structure (a stray colon, quote, or newline).
    config_path.write_text(
        _CONFIG_TEMPLATE.format(namespace=json.dumps(namespace)), encoding="utf-8"
    )
    ignore_path.write_text(_IGNORE_TEMPLATE, encoding="utf-8")
    log(f"wrote {config_path}")
    log(f"wrote {ignore_path}")
