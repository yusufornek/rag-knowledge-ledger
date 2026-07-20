# RAG Knowledge Ledger

RAG Knowledge Ledger is an open source lineage and integrity platform for
retrieval-augmented generation (RAG) knowledge bases. It records, signs, and
verifies which exact source document version, parser, chunker, and embedding
configuration produced each indexed point in a vector index, and reconciles
that record against the actual state of the index.

## What this project does

Vector database points are typically an id, a vector, and a free-form JSON
payload. RAG ingestion pipelines add metadata, but nothing guarantees that
metadata is complete, current, or still connected to its source document.
Sources get updated while stale chunks remain indexed, documents get deleted
while their vectors survive, and embedding configuration changes without a
full rebuild, all silently. RAG Knowledge Ledger addresses this by treating
lineage as content-addressed evidence rather than as a dashboard: source
snapshots, parse output, chunk identity, embedding identity, and indexed
point locators are each recorded as their own manifest entries, hashed, and
optionally signed with Ed25519.

The system builds a deterministic manifest from a source tree (RAG Ledger
Manifest v1), reads the observed inventory of a Qdrant collection or a
pgvector table through read-only connectors, and reconciles the two to
report stale, orphaned, missing, duplicate, metadata-mismatched, and
ACL/tenant-drifted points against a defined taxonomy. A policy engine turns
those findings into pass/warn/fail verdicts suitable for a CI gate. PII
scanning, SPDX license assertions, and ACL/tenant checks are attached to the
same lineage evidence so governance questions ("was this chunk scanned for
PII before it was indexed", "what license applies to this source") can be
answered from the manifest itself, not reconstructed after the fact.

This is not a vector database, not a RAG chatbot or retriever, and not a
general data catalog. It fills a narrower gap: a portable, signable manifest
format and a deterministic reconciliation engine for RAG knowledge bases.

## Project status

This project is in early development toward v0.1.0. The manifest format,
pipeline, connectors, and reconciliation engine described above are the
target scope for v0.1.0; most of it does not exist yet. See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for the current,
honest, requirement-by-requirement status.

Currently implemented:

- Project scaffolding: `uv`-managed Python package layout (`src/ragledger`)
  with a `ragledger` CLI entry point exposing a `version` command.
- Continuous integration: lint, type check, and test workflow.
- Pre-code design documents: architecture decision records, threat model,
  manifest v1 and policy v1 JSON Schemas, and the test/evidence matrix.

Everything else described above (source discovery, parsing, chunking,
embedding, signing, connectors, reconciliation, policy evaluation, CLI
commands beyond `version`, API, and web UI) is planned and not yet
implemented. The manifest and policy JSON Schemas under `docs/spec/` are
design artifacts derived from the project specification; no code currently
produces or consumes them.

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer
(the repository pins 3.13 in `.python-version`).

```bash
uv sync --dev
uv run pytest
```

Other useful commands during development:

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting check
uv run mypy src               # type check
```

A `docker-compose.yml` is provided for local integration testing against
real Qdrant and pgvector instances once the connectors that use them are
implemented. It is not required for the commands above and is not started
as part of this repository's setup.

## License

Apache License 2.0. See [LICENSE](LICENSE).
