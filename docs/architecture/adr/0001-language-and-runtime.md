# ADR 0001: Language, runtime, and project layout

## Status

Accepted

## Context

RAG Knowledge Ledger needs a deterministic core (canonicalization, hashing,
signing, reconciliation), untrusted-document parsing, a CLI, and later an
HTTP API and background workers. The project specification requires the
CLI and Python package to be named `ragledger`, requires a Python SDK for
instrumenting existing ingestion pipelines, and lists `psycopg`, `httpx`,
`cryptography`, and Sentence Transformers-family embedding models among
its dependencies, all of which are Python-first ecosystems. The
specification also calls for reproducible builds, strict typing, and a
project layout that is straightforward for external open source
contributors to build and test.

## Decision

- The project is implemented in Python, with `requires-python = ">=3.11"`.
  3.11+ is chosen for match statements, exception groups, and
  performance improvements relevant to the streaming/hashing workloads
  in this project, while remaining broadly available in current Linux
  distributions and container base images.
- Dependency and environment management uses `uv`, with a committed
  `uv.lock` for reproducible installs and CI runs. `uv` is also used to
  run the project's dev tools (`uv run pytest`, `uv run ruff`, `uv run
  mypy`) so contributors need no separate virtualenv bootstrapping step.
- The package uses a `src/` layout (`src/ragledger/`) rather than a
  flat layout, so the installed package is exercised the same way in
  tests as it would be by an external consumer, and so the repository
  root is not accidentally importable.
- The CLI is built with `typer`, matching the project specification's
  CLI/package naming (`ragledger`) and giving a typed, testable command
  structure that can grow into the standalone and server-backed modes
  described in the specification.
- Static typing is enforced with `mypy --strict` on `src/`, and
  linting/formatting with `ruff`, both run in CI on every push to `main`
  and every pull request across Python 3.11, 3.12, and 3.13.

## Consequences

- Contributors need `uv` installed locally; the README documents this
  as the only required setup step.
- A `src/` layout means editable installs are required during
  development (`uv sync` handles this); this is standard for modern
  Python packaging and has no meaningful downside here.
- Committing `uv.lock` means dependency upgrades are explicit, reviewed
  changes rather than silent floating-version drift, consistent with
  the specification's requirement that versions be pinned.
- Choosing Python for the whole standalone core (as opposed to a
  compiled language for the reconciliation engine) trades some raw
  performance for a single-language codebase and a much lower barrier
  to external contribution; the 1M-point performance targets in the
  specification will need explicit streaming and memory-bounded
  algorithms rather than relying on raw interpreter speed.
