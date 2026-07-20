# Contributing to RAG Knowledge Ledger

Thank you for considering a contribution. This document describes how
external contributors work with this repository.

## Workflow

This project uses the standard fork-and-pull-request workflow:

1. Fork the repository.
2. Create a branch in your fork for your change.
3. Make your change, with tests.
4. Open a pull request against `main` in the upstream repository.
5. Address review feedback.

Do not commit directly to `main` in the upstream repository; only the
project's own release process does that.

## Development setup

```bash
uv sync --dev
uv run pytest
```

## Code style

- Formatting and linting are enforced with [ruff](https://docs.astral.sh/ruff/):
  `uv run ruff check .` and `uv run ruff format --check .` must pass.
- Type checking is enforced with mypy: `uv run mypy src` must pass.
- New code targets Python 3.11+ and uses type hints throughout.
- Keep functions and modules focused; prefer explicit code over clever code.

## Tests

- Every change that adds or modifies behavior needs a corresponding test.
- Run the full test suite with `uv run pytest` before opening a pull
  request. CI runs the same commands on Python 3.11, 3.12, and 3.13.
- Fixtures used in tests must be either synthetic or under a license that
  permits redistribution in this repository. Do not add copyrighted or
  proprietary documents as test fixtures.

## Adding a new parser, chunker, embedding, or connector adapter

Adapters implement the port interfaces described in the project
specification. When proposing a new adapter:

- Implement the required interface methods completely; partial adapters
  that silently no-op are not accepted.
- Add contract tests that exercise the adapter against the same fixture
  set used by existing adapters of the same kind, so behavior can be
  compared for parity.
- Document configuration options and any external dependencies (models,
  binaries, network access) in the adapter's module docstring.
- Connector adapters must be read-only against the target system: no
  code path may issue a mutating request (write, delete, DDL) against a
  configured target.

## Manifest and schema compatibility

Changes to `docs/spec/manifest-v1.schema.json` or
`docs/spec/policy-v1.schema.json` are backward-compatibility-sensitive.
Any change to a committed schema must include:

- A rationale for the change in the pull request description.
- Updated fixtures and tests that reflect the new schema.
- A note in `CHANGELOG.md` under the `Unreleased` section.

Breaking changes to manifest v1 are out of scope for this repository's
v1.x releases; propose a new manifest version instead.

## Security issues

Do not open a public issue for a suspected security vulnerability. See
[SECURITY.md](SECURITY.md) for the reporting process.

## Commit and pull request messages

Write plain, descriptive, imperative-mood messages (for example, "Add
pgvector snapshot pagination"). Do not include AI/agent attribution,
generated-by notices, or co-author trailers referencing automated tools.

## Code of conduct

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).
