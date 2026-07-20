# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project scaffolding: `uv`-managed Python package layout (`src/ragledger`)
  with a `ragledger` CLI entry point.
- Continuous integration workflow (lint, type check, test on Python 3.11,
  3.12, and 3.13).
- Pre-code design documents: architecture decision records, threat model,
  manifest v1 and policy v1 JSON Schemas, and the test/evidence matrix.
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
