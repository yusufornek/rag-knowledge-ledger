# M4 status notes: CLI build/report

Scope: `src/ragledger/cli/`, `src/ragledger/reports/`, `tests/cli/`,
`tests/reports/`. Plain status notes for the orchestrator to merge into
`IMPLEMENTATION_STATUS.md`/`CHANGELOG.md`; this file is not itself the
status ledger. `src/ragledger/reconcile/` (owned by a concurrently
developed agent) was not read for design purposes and was not touched;
no `reconcile` command was registered.

## Commands implemented

`pyproject.toml`'s `[project.scripts]` entry (`ragledger =
"ragledger.cli:app"`) is unchanged; `src/ragledger/cli.py` (a single
module, M0-era, only `version`) was replaced by the package
`src/ragledger/cli/` whose `__init__.py` defines the same `app` object.

| Command | Signature | File |
|---|---|---|
| `ragledger version` | `ragledger version` | `cli/__init__.py` |
| `ragledger init` | `ragledger init [DIRECTORY] [--namespace TEXT] [--force]` | `cli/commands/init_cmd.py` |
| `ragledger build` | `ragledger build PATH --config ragledger.yml --output manifest.json [--artifacts DIR] [--cache DIR] [--epoch INT] [--reproducible/--no-reproducible] [--allow-incomplete]` | `cli/commands/build_cmd.py` |
| `ragledger manifest validate` | `ragledger manifest validate MANIFEST` | `cli/commands/manifest_cmd.py` |
| `ragledger manifest sign` | `ragledger manifest sign MANIFEST --key-file KEY [--output PATH] [--issuer TEXT] [--epoch INT]` | `cli/commands/manifest_cmd.py` |
| `ragledger manifest verify` | `ragledger manifest verify MANIFEST [--public-key PATH]... [--deep] [--artifacts DIR]` | `cli/commands/manifest_cmd.py` |
| `ragledger key generate` | `ragledger key generate [--private-key-file PATH] [--public-key-file PATH] [--force]` | `cli/commands/key_cmd.py` |
| `ragledger target add` | `ragledger target add {qdrant\|pgvector} --config PATH [--check/--no-check] [--expected-dimension INT]` | `cli/commands/target_cmd.py` |
| `ragledger snapshot` | `ragledger snapshot TARGET --output OUT.ndjson.zst [--checkpoint JSON] [--resume] [--include-vectors] [--epoch INT]` | `cli/commands/snapshot_cmd.py` |
| `ragledger report manifest` | `ragledger report manifest MANIFEST [--format json\|html] [--output PATH]` | `cli/commands/report_cmd.py` |
| `ragledger report snapshot` | `ragledger report snapshot SNAPSHOT.ndjson.zst [--format json\|html] [--output PATH]` | `cli/commands/report_cmd.py` |

Not implemented (out of this milestone's deliverable list; do not
appear in the CLI): `reconcile` (owned elsewhere per instructions),
`inspect chunk`, `diff`, `doctor`, `serve` (all listed in
PROJECT_SPEC.md section 17.1 but not in this milestone's task
description).

## Files created

- `src/ragledger/cli/__init__.py`, `_exit.py`, `_output.py`, `_config.py`,
  `_build_support.py`, `_target.py`
- `src/ragledger/cli/commands/__init__.py`, `init_cmd.py`, `build_cmd.py`,
  `manifest_cmd.py`, `key_cmd.py`, `target_cmd.py`, `snapshot_cmd.py`,
  `report_cmd.py`
- `src/ragledger/reports/__init__.py`, `_html.py`, `manifest_report.py`,
  `snapshot_report.py`
- `tests/cli/conftest.py`, `test_init.py`, `test_build.py`,
  `test_manifest.py`, `test_key.py`, `test_target.py`, `test_snapshot.py`,
  `test_report.py`
- `tests/reports/conftest.py`, `test_manifest_report.py`,
  `test_snapshot_report.py`
- `src/ragledger/cli.py` (the old single-module CLI) was removed;
  `src/ragledger/cli/__init__.py` is its replacement.

No file outside these paths was modified. `pyproject.toml` was not
touched (the existing `[project.scripts]` entry already resolves
correctly against a package).

## Spec interpretation decisions

1. **Exit codes beyond the literal section 17.1 table.** The table
   defines eight codes but several commands here produce outcomes the
   table doesn't explicitly enumerate:
   - `build`: a `build.status == "incomplete"` manifest (parse
     failures) exits `3` ("Policy fail"), per section 10.2's own words
     ("policy default fail"), unless `--allow-incomplete` is passed
     (exit `0`, manifest still written either way).
   - `manifest verify`: `VALID_TRUSTED` -> `0`; `VALID_UNTRUSTED` -> `2`
     ("Findings var, gate fail değil" -- a valid signature from an
     unrecognized key is a finding, not tampering); `INVALID` /
     `INCOMPLETE` -> `5`; a `--deep` artifact-hash mismatch always forces
     `5` regardless of signature outcome.
   - Click/Typer's own argument-parsing usage errors (missing required
     argument, wrong option type) exit with Click's built-in code `2`
     before this project's command bodies ever run. This numerically
     collides with this project's own exit-`2` meaning but is an
     unavoidable artifact of the underlying argument parser, not a
     deliberate choice; documented here rather than silently accepted.

2. **`key generate` is this milestone's addition.** Section 17.1's
   command list shows `manifest sign --key-file`/`manifest verify
   --public-key` consuming already-existing key files but no
   standalone generation command. Without `key generate`, `sign`/`verify`
   have no way to produce a first keypair from the CLI alone.

3. **`embedding.mode` in `ragledger.yml` only supports `none`,
   `deterministic`, and `local` in this release**, not a live
   `sentence-transformers`/OpenAI call. `local` still validates
   `model-revisions.lock` exactly as section 17.3 specifies (missing
   file or mutable-alias revision is rejected at config-validation
   time, before any pipeline work starts), but the vectors it actually
   produces come from
   `ragledger.pipeline.embedding.DeterministicLocalEmbeddingProvider`,
   the only network-free, fully working provider this codebase ships
   (`SentenceTransformersEmbeddingProvider`/`OpenAiEmbeddingProvider`
   both raise `ProviderNotAvailableError` unconditionally in this
   release -- wiring `local` mode to them would crash every build).
   `ragledger build` logs this substitution explicitly rather than
   silently pretending real inference happened. `ragledger init`'s
   generated skeleton defaults to `mode: deterministic` (no lock file
   needed) rather than section 17.3's literal `mode: local` example, so
   `ragledger init && ragledger build .` works with no extra setup; the
   literal spec shape is shown commented-out in the generated file.

4. **`parser:`/`chunker.tokenizer:` config fields are validated for
   shape but not fully wired.** Every native parser this codebase ships
   (`ragledger.pipeline.parsers.*`) declares an empty allowed-config-key
   set -- there is no Docling/OCR adapter -- so `parser.ocr` is never
   forwarded as `parser_config` (it would raise `unknown parser config
   keys`). `chunker.tokenizer` (a model name) is accepted but not
   forwarded either: only `WhitespaceTokenizer` is wired in
   `ragledger.pipeline.chunkers.base.resolve_tokenizer`. `chunker.strategy`
   /`max_tokens`/`overlap_tokens` map directly and do have real effect.

5. **`governance.acl_required` has no observable effect on a build.**
   `ragledger.governance.acl.AclConfig` has no `required`-style
   enforcement flag analogous to `TenantConfig.required` (which does
   produce a real `TENANT_REQUIRED_BUT_MISSING` warning per source when
   `tenant_required: true` and no tenant resolves). `ragledger.yml`'s
   flat schema (matching section 17.3's literal example exactly) has no
   nested block for declaring actual ACL source entries/path rules, so
   there is nothing for `acl_required` to enforce against yet;
   `ragledger build` logs this explicitly rather than fabricating ACL
   assertions or silently ignoring the flag without comment.

6. **Checkpoint/resume for `ragledger snapshot` is scoped to one
   synchronous invocation**, not a background/interruptible daemon.
   `--checkpoint` passes an explicit JSON-encoded resume token straight
   to the connector; after any run that observed a point, a
   `<output>.checkpoint.json` sidecar records the last point's
   checkpoint value for a later `--resume` run to pick up from. This is
   verified correct against `NdjsonConnector` (its checkpoint format --
   a canonical-JSON string key of `point_id` -- is exactly what
   `ragledger.connectors.ndjson` documents, and this milestone's tests
   exercise resume, explicit-checkpoint, and malformed-checkpoint
   paths). For Qdrant/pgvector the sidecar stores the raw `point_id`
   value as a best-effort checkpoint; this is not verified against a
   live target here (no live service is available or permitted in this
   test suite). A resumed run overwrites `--output` with only the newly
   read tail, not a merge with the prior file.

7. **`target add ndjson` does not exist; `snapshot`'s target config
   supports a CLI-only `type: ndjson` shape** (`ragledger.cli._target.NdjsonSourceConfig`)
   that replays an existing `.ndjson.zst` file through
   `NdjsonConnector`, exactly matching that connector's own documented
   purpose ("lets reconciliation and this milestone's tests treat a
   committed NDJSON fixture exactly like a live Qdrant or pgvector
   connection"). This is what makes `tests/cli/test_snapshot.py`
   possible with the committed fixtures and no live service.

8. **Report scope is `report manifest`/`report snapshot` only**, not
   PROJECT_SPEC.md section 8.15's SARIF/JUnit/policy-findings reporting
   (FR-140-144): those require reconciliation/policy-evaluation output
   that does not exist yet (M6 scope). Section 23's HTML/JSON
   requirements that do apply to a manifest/snapshot summary --
   self-contained (inline CSS, no external assets, no JS), masked-only
   PII evidence, bounded point sampling for snapshots -- are
   implemented; see `src/ragledger/reports/`.

## Verification results

All commands run from the repository root, own paths only:

```
uv run ruff check src/ragledger/cli src/ragledger/reports tests/cli tests/reports
  -> All checks passed!
uv run ruff format --check src/ragledger/cli src/ragledger/reports tests/cli tests/reports
  -> 29 files already formatted
uv run mypy src/ragledger/cli src/ragledger/reports
  -> Success: no issues found in 18 source files
uv run pytest tests/cli tests/reports -q
  -> 76 passed
```

`ruff`'s `B008` (function call in argument default) fires on every
`typer.Option(...)`/`typer.Argument(...)` default whose annotated type
is `Path`/`Path | None`/`list[Path]` (empirically, scalar types --
`str`, `int`, `bool`, and `Optional` of those -- are exempted by ruff's
own heuristic, `Path`/`list[...]` are not). This is inherent to how
Typer's CLI parameter declarations work (the default *is* the metadata
object Typer inspects; there is no way to "perform the call within the
function" as the rule's message suggests without breaking Typer's
introspection). Each such line carries an explicit `# noqa: B008`
rather than a blanket per-file/pyproject suppression, since editing
`pyproject.toml`'s ruff configuration was out of scope for this
milestone.

As a non-regression check (read-only, no files touched), the rest of
the existing suite was also run and is unaffected by the
`cli.py` -> `cli/` package restructuring:

```
uv run pytest tests/test_package.py tests/core tests/pipeline tests/governance -q
  -> 425 passed
uv run pytest tests/connectors -q
  -> 162 passed, 4 skipped (live-integration tests, gated on RAGLEDGER_IT=1, unchanged)
```

## Test coverage highlights

- `tests/cli/test_build.py::test_build_twice_with_the_same_epoch_is_byte_identical`
  builds the corpus twice with the same `--epoch` into separate output
  files and asserts the manifest bytes are identical -- the deliverable's
  explicit "build twice -> byte-identical manifests" requirement.
- `tests/cli/test_manifest.py` and `tests/cli/test_key.py` cover
  validate (valid + schema-invalid + corrupted-hash), sign, and verify
  across all four `VerificationOverall` outcomes (`VALID_TRUSTED`,
  `VALID_UNTRUSTED` via an unrecognized key, `INVALID` via
  post-signing tampering, `INCOMPLETE` via an unsigned manifest), plus
  `--deep` artifact verification (pass and a missing-artifact failure).
- `tests/cli/test_snapshot.py` exercises `NdjsonConnector`-backed
  full/partial/resumed passes, both committed fixtures (integer Qdrant
  point ids and composite pgvector point ids), and every
  config/checkpoint error path.
- `tests/cli/test_report.py::test_report_no_canary_pii_value_leaks_into_json_or_html`
  and `tests/reports/test_manifest_report.py::test_no_canary_pii_value_appears_in_json_or_html_report`
  are PII-leak canary tests in the same spirit as
  `tests/governance/test_pii_leak_canary.py`: they build a manifest over
  the corpus (which contains known, synthetic canary PII values),
  generate both report formats, and assert the raw canary strings never
  appear, while separately asserting detection is actually live (at
  least one `EMAIL_ADDRESS` finding present) so an absent-canary
  assertion can't be vacuously true.
- HTML self-containment is asserted directly: no `<script`, no
  `http://`/`https://` substring, and (in `tests/reports/`) every
  codepoint below the emoji/pictograph Unicode block.

## Honest gaps

- **FR-080** (build plan/dry-run preview: source count, estimated
  parse/chunk/embed cost, resource caps before running) is not
  implemented. `ragledger build` runs the pipeline directly; there is
  no `--dry-run`/`plan` mode.
- **FR-071**'s "public export" ACL-principal redaction is not
  implemented; this CLI never redacts/hashes ACL principal identifiers
  differently for any export path (reports include raw ACL entry
  strings as recorded in the manifest, same as the manifest itself
  does).
- **`snapshot`'s checkpoint/resume for Qdrant/pgvector** is
  implemented but not verified against a live target (see interpretation
  decision 6); only the `NdjsonConnector` path is test-covered.
- **`target add`'s `--check` preflight** (`ragledger.connectors.config.run_preflight`)
  is exercised in tests only against an intentionally unreachable
  loopback endpoint (fast `ECONNREFUSED`, no real network); a
  successful reachable/authenticated preflight path has no test here,
  consistent with the "no live services in tests" constraint.
- **`ragledger.yml` has no nested ACL source-entry/path-rule config
  surface** (see interpretation decision 5); only `governance.acl_required`
  exists as a flat boolean with no enforcement behavior yet.
- **`model-revisions.lock`'s `files` (per-file checksum) block** is
  validated only for shape (non-empty string values); it is never
  resolved against an actual downloaded model file, since no code path
  in this release downloads or loads a real model.
