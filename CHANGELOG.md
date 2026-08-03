# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## 0.1.1 - 2026-07-21

### Fixed

- Source discovery now accesses files through their original operating system
  paths while keeping NFC-normalized identity paths. Discovery previously
  failed with `FileNotFoundError` on non-normalizing filesystems (for example
  ext4 on Linux) when a file name contained decomposed Unicode (NFD).
- pgvector schema inspection now reads the declared dimension of `vector`,
  `halfvec`, and `sparsevec` columns correctly. The pgvector extension
  stores the dimension directly in `atttypmod`, unlike varlena types that
  add a 4-byte header offset, so a `vector(3)` column previously reported
  dimension -1. Found by the live integration test suite against a real
  pgvector instance.

## 0.1.0 - 2026-07-21

### Added

- Project scaffolding: `uv`-managed Python package layout (`src/ragledger`)
  with a `ragledger` CLI entry point.
- Continuous integration workflow (lint, type check, test on Python 3.11,
  3.12, and 3.13).
- Pre-code design documents: architecture decision records, threat model,
  and the manifest v1 and policy v1 JSON Schemas.
- Open source project governance files: `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, issue and pull request templates.
- Identity and manifest core (`ragledger.core`): RFC 8785 canonical JSON
  serialization, SHA-256 content hashing, stable content-derived record
  IDs, pydantic v2 manifest v1 models for every record and assertion
  type, manifest assembly with JSON Schema validation, Ed25519 manifest
  signing and verification with distinct valid/tampered/untrusted-key
  outcomes, and a content-addressed local artifact store. Includes a
  three-manifest golden fixture corpus under `tests/fixtures/golden/`
  with byte-identical determinism tests.
- Source/parse/chunk pipeline (`ragledger.pipeline`): filesystem source
  discovery with `.gitignore`/`.ragledgerignore` rules, streaming
  content hashing, media type sniffing, duplicate-content relationships,
  and tombstone detection; a `DocumentParser` adapter contract with
  native deterministic parsers for plain text, Markdown (including YAML
  frontmatter and `SPDX-License-Identifier` header detection), HTML
  (built on the standard library's `html.parser`), JSON, CSV, and PDF
  (`pypdf`-backed); a subprocess sandbox that isolates untrusted
  parsing with a timeout and output-size cap, degrading a
  hanging/crashing/oversized-output parser to a failure record instead
  of a pipeline crash; a `Chunker` adapter contract with built-in
  `hierarchical`, `hybrid`, and `line_based` strategies, a declarative
  contextualization template renderer, oversized-element
  split/fail handling, and table-header repetition across split chunks;
  an `EmbeddingProvider` adapter contract with a deterministic seeded
  hash-projection reference embedder (explicitly not a semantic model),
  an external-vector-import provider, and config-plumbing-only
  Sentence Transformers/OpenAI provider stubs that make no live calls;
  content-addressed pipeline stage caching; and a `build_pipeline`
  orchestrator producing byte-identical canonical manifests across
  repeated runs in reproducible mode.
- Governance (`ragledger.governance`): deterministic PII detection
  (email, phone, IBAN, Luhn-validated credit card, US SSN, Turkish
  TCKN) with masked-preview and HKDF/HMAC-based evidence -- never a raw
  value or a plain hash of one -- plus YAML-configurable custom
  recognizers with regex-timeout protection; SPDX license assertion
  with a documented precedence order across user assertion, sidecar,
  frontmatter, path rule, and repository default, and cross-referenced
  conflict findings; and ACL/tenant assertion construction with the
  canonical `PUBLIC`/`USER:`/`GROUP:`/`ROLE:`/`ATTRIBUTE:` entry
  grammar, deny-entry rejection, and case-sensitive-by-default
  normalization. Includes a synthetic, fully fake document corpus under
  `tests/fixtures/corpus/` (text, Markdown, HTML, JSON, CSV, and a
  programmatically generated PDF) and a PII leak canary test asserting
  every synthetic canary value is absent from the produced manifest.
- Command-line interface (`ragledger.cli`): `ragledger init`, `build`,
  `manifest validate|sign|verify`, `key generate`, `target add`,
  `snapshot`, `report manifest|snapshot`, and `reconcile`. `manifest
  verify --deep` additionally checks artifact bytes against their
  declared hash; `build` exits non-zero on an incomplete manifest unless
  `--allow-incomplete` is passed; `snapshot` supports
  `--checkpoint`/`--resume` with a JSON checkpoint sidecar. Two build
  runs with the same `--epoch` produce byte-identical manifests.
- Manifest and snapshot reporting (`ragledger.reports`): self-contained,
  dependency-free JSON and HTML report renderers (no external CSS/JS/
  fonts, no `<script>` tag) for a manifest's build/source/governance
  summary and a snapshot's header/trailer/point statistics, both backed
  by the same fact-collection code the JSON and HTML output share.
- Vector database connectors (`ragledger.connectors`): read-only Qdrant
  (REST, scroll-based pagination, collection alias resolution) and
  pgvector (`psycopg`, server-side named cursor with keyset pagination,
  composite primary keys as canonical JSON point ids) connectors, plus
  an NDJSON snapshot format (zstd-compressed, one immutable
  content-hashed file per pass) and its own `NdjsonConnector` for
  replaying a committed snapshot as if it were a live target. Every
  connector normalizes to a common `NormalizedPoint` shape, supports
  checkpoint/resume, reports its consistency mode and completeness
  (`strict_consistent`/`best_effort_paged`/`best_effort_live`), and is
  enforced read-only at the transport layer: Qdrant via an `httpx`
  request allowlist, pgvector via both a statement allowlist and a
  database-level read-only transaction. Neither connector ever issues a
  mutating request or SQL statement; there is no mutation method on the
  connector interface at all.
- Reconciliation and policy engine (`ragledger.reconcile`): a 23-code
  finding taxonomy matching `docs/spec/policy-v1.schema.json`'s enum;
  a streaming reconciliation engine with both an in-memory small-data
  path and a bounded-memory external-merge big-data path (spilled,
  sorted, k-way-merged run files) that produce identical findings for
  the same input; policy v1 document loading and evaluation
  (PASS/WARN/FAIL/INCONCLUSIVE verdicts over findings severity, PII,
  license, ACL/tenant, and drift-ratio rules); a read-only remediation
  planner that only ever proposes candidate actions, never executes
  one, and flags destructive candidates with an explicit caution; and
  canonical JSON/CI-text report rendering. Every ACL principal
  identifier in a finding's evidence is masked (HMAC-keyed when a
  workspace secret is supplied, else SHA-256) before it is ever
  constructed, and PII evidence carries only entity type, confidence,
  and an already-masked preview -- never a raw value -- verified by a
  dedicated masking canary test.
- `ragledger reconcile MANIFEST SNAPSHOT [--policy FILE] [--output
  report.json] [--html report.html] [--work-dir DIR]
  [--big-data|--auto]`: wires the reconciliation/policy/remediation
  engine into the CLI, auto-selecting the big-data path for a snapshot
  too large to hold in memory, printing a plain-text CI summary to
  stdout, and exiting `0` on a policy pass (or no policy given), `1` on
  a policy fail, `2` on an execution error. A self-contained HTML
  reconciliation report renderer (`ragledger.reports.reconciliation_report`)
  joins the existing manifest/snapshot report pair.

### Fixed

- Removed a "Generated by ragledger report." footer line from every
  HTML report (manifest, snapshot, reconciliation): found during the
  v0.1.0 release security review's attribution scan, which flagged the
  phrase even though it referred to the tool itself, not an AI system.
  Reports now end with a plain "ragledger report." label instead.
