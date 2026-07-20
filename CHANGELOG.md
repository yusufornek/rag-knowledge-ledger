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
