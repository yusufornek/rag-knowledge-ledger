# RAG Knowledge Ledger

RAG Knowledge Ledger is a lineage and drift-audit tool for
retrieval-augmented generation (RAG) knowledge bases. It builds a
deterministic, content-hashed, optionally Ed25519-signed manifest of a
document source tree (which files, which parser/chunker/embedding
configuration, which resulting chunks and embeddings), reads the observed
inventory of a Qdrant collection or a pgvector table through read-only
connectors, and reconciles the two: which points are missing from the
index, which are orphaned in it, which are stale relative to a source
that has since changed, which have drifted ACL/tenant/payload metadata.
A policy engine turns those findings into a PASS/WARN/FAIL verdict
suitable for a CI gate.

The problem it addresses: a vector index point is typically an id, a
vector, and a free-form payload. Nothing about that shape guarantees the
payload is still accurate, that the source document it was built from
still exists in its original form, or that access-control metadata
still matches the source's actual policy. RAG Knowledge Ledger treats
that lineage as content-addressed evidence -- source snapshots, parse
output, chunk identity, embedding identity, and index point locators
are each their own hashed manifest record -- rather than as a dashboard
assembled after the fact.

This is not a vector database, not a RAG chatbot or retriever, and not a
general data catalog or ETL tool. It does not write to any index, ever;
every connector is read-only. See [Limitations](#limitations) below for
what is explicitly out of scope for v0.1.0.

## Status

v0.1.0. See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for the
full, requirement-by-requirement status against the project
specification, and [CHANGELOG.md](CHANGELOG.md) for the release history.

## Features

What v0.1.0 actually ships, as a CLI (`ragledger ...`) and a Python
library (`import ragledger`):

- **Manifest core**: RFC 8785 canonical JSON, stable content-derived
  record ids, a pydantic-validated manifest schema, Ed25519 signing and
  verification, and a content-addressed local artifact store.
- **Build pipeline**: filesystem source discovery (`.gitignore`/
  `.ragledgerignore`-aware), native deterministic parsers for plain
  text, Markdown, HTML, JSON, CSV, and PDF, running inside a sandboxed
  subprocess with a timeout and output cap; hierarchical/hybrid/
  line-based chunking; a deterministic reference embedder; and a
  `build_pipeline` orchestrator that produces byte-identical canonical
  manifests across repeated runs of the same input.
- **Governance**: deterministic PII detection (email, phone, IBAN,
  credit card, US SSN, Turkish TCKN) with masked-only evidence, never a
  raw value; SPDX license assertion with a documented source
  precedence; ACL/tenant assertion construction with a canonical entry
  grammar.
- **Connectors**: read-only Qdrant and pgvector connectors (scroll/
  keyset pagination, checkpoint/resume, consistency reporting), plus a
  self-contained, content-hashed, zstd-compressed NDJSON snapshot
  format usable offline or as a CI fixture.
- **Reconciliation and policy**: a 23-code finding taxonomy, a
  streaming reconciliation engine with both an in-memory path and a
  bounded-memory external-merge path for large snapshots, policy v1
  document evaluation to a PASS/WARN/FAIL/INCONCLUSIVE verdict, and a
  read-only remediation planner that only ever proposes candidates.
- **CLI**: `init`, `build`, `manifest validate|sign|verify`, `key
  generate`, `target add`, `snapshot`, `report manifest|snapshot`, and
  `reconcile`, each documented in the [Quickstart](#quickstart) below.
- **Reports**: self-contained JSON and HTML reports (no external
  assets, no `<script>` tag) for a manifest, a snapshot, and a
  reconciliation result.

## Install

Not published to PyPI. Install from source with [uv](https://docs.astral.sh/uv/)
(recommended) or `pip`, against Python 3.11 or newer (the repository
pins 3.13 in `.python-version`).

From a local clone of this repository:

```bash
cd rag-knowledge-ledger

# uv (recommended): creates .venv and installs the ragledger CLI into it
uv sync
uv run ragledger version

# or, with pip, in your own virtual environment
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
ragledger version
```

## Quickstart

Every command below was actually run end to end while writing this
document (in a throwaway directory, deleted afterward); the outputs are
real, only trimmed for length. `--epoch 0` is used throughout so the
output is reproducible instead of timestamp-dependent -- omit it in
normal use to record the real current time.

Set up a tiny two-document source tree:

```bash
mkdir -p demo/documents && cd demo
cat > documents/refund-policy.md <<'EOF'
---
title: Refund Policy
license: CC-BY-4.0
---

# Refund Policy

Refunds are available within 30 days of purchase for any unused
license seat. Contact support to start a refund request.
EOF
cat > documents/support-hours.txt <<'EOF'
Support hours are Monday through Friday, 9am to 6pm UTC.
Response time target for a new ticket is one business day.
EOF
```

**1. Initialize a config** (`ragledger init`):

```bash
$ ragledger init . --namespace demo-kb
wrote demo/ragledger.yml
wrote demo/.ragledgerignore
```

**2. Build a manifest** (`ragledger build`) -- discovers, parses,
chunks, PII/license/ACL-scans, and embeds every source under
`documents/`, writing a canonical, schema-valid manifest:

```bash
$ ragledger build ./documents --config ragledger.yml --output manifest.json \
    --artifacts .ragledger/artifacts --cache .ragledger/cache --epoch 0
building namespace='demo-kb' root=documents build_id=bld_19700101T000000Z reproducible=True
wrote manifest.json: sources=2 chunks=3 embeddings=3 assertions=8 warnings=0 status=complete manifest_hash=d265fa26...
```

**3. Validate, sign, and verify** the manifest:

```bash
$ ragledger manifest validate manifest.json
manifest.json: valid. namespace='demo-kb' build_status=complete sources=2 chunks=3 embeddings=3 signatures=0 manifest_hash=d265fa26...

$ ragledger key generate --private-key-file signing.key --public-key-file signing.pub
wrote signing.key (mode 0600) and signing.pub
key id (sha256 fingerprint): bb5c2139...

$ ragledger manifest sign manifest.json --key-file signing.key --issuer quickstart --epoch 0
wrote manifest.json: signed with key_id=bb5c2139... issuer='quickstart' signed_at=1970-01-01T00:00:00+00:00

$ ragledger manifest verify manifest.json --public-key signing.pub
signature key_id=bb5c2139... status=valid
overall=VALID_TRUSTED hash_valid=True signatures=1
```

**4. Render an HTML report** of the manifest:

```bash
$ ragledger report manifest manifest.json --format html --output manifest-report.html
wrote manifest-report.html
```

**5. Capture a snapshot of a target's inventory** (`ragledger
snapshot`). This quickstart has no live Qdrant/pgvector service, so it
uses the CLI-only `type: ndjson` target shape to replay an existing
`.ndjson.zst` file as if it were a live connector -- exactly the same
code path a real Qdrant/pgvector capture uses, just without the
network. (`fake-index-source.ndjson.zst` here stands in for "an
already-existing index"; a few lines of Python using
`ragledger.connectors.ndjson.write_snapshot` produced it, mirroring
what `ragledger.connectors.qdrant`/`pgvector`'s own `iterate_points`
would otherwise supply.)

```bash
$ cat > ndjson-target.yml <<'EOF'
type: ndjson
path: fake-index-source.ndjson.zst
EOF

$ ragledger snapshot ndjson-target.yml --output observed.ndjson.zst --epoch 0
wrote resume checkpoint to observed.ndjson.zst.checkpoint.json
wrote observed.ndjson.zst: points=1 consistency=complete mode=strict_consistent content_hash=7f5a72df...
```

**6. Reconcile** the manifest against the snapshot under a simple
policy, and let it act as a CI gate:

```bash
$ cat > policy.yml <<'EOF'
version: 1
name: demo-ci-gate
requirements:
  manifest_signature: required
findings:
  fail_on_severity: [critical, high]
  warn_on_severity: [medium]
EOF

$ ragledger reconcile manifest.json observed.ndjson.zst --policy policy.yml \
    --output reconcile-report.json --html reconcile-report.html
wrote reconcile-report.json
wrote reconcile-report.html
reconciliation: target=demo-qdrant scope=support-kb verdict=FAIL
points: expected=0 observed=1 matched=0
findings: high=1
ratios: lineage_coverage=1.0000 missing_ratio=not_applicable orphan_ratio=1.0000 stale_ratio=not_applicable acl_compliance=not_applicable
policy rule [FAIL] findings.fail_on_severity: 1 finding(s) at a fail-gated severity ['critical', 'high']
remediation: delete_point_candidate target=demo-qdrant scope=support-kb candidates=1 destructive=True
exit_code=1

$ echo "shell exit code: $?"
shell exit code: 1
```

That `ORPHAN_IN_INDEX` finding and the resulting `FAIL`/exit-`1` are
real and expected here: the simulated "existing index" point has a
`source_id` this manifest has no record of, exactly the drift this tool
is meant to catch. This particular demo's `expected=0` reflects a real
limitation, not a hidden step -- see
[Index bindings are not produced by `build`](#limitations).

## Architecture

```
src/ragledger/
  core/        Canonical JSON, content-derived IDs, manifest v1 models,
               Ed25519 signing/verification, content-addressed artifacts.
  pipeline/    Source discovery, sandboxed parsers, chunkers, the
               deterministic reference embedder, stage caching, and the
               build_pipeline orchestrator.
  governance/  PII detection, SPDX license resolution, ACL/tenant
               assertion construction.
  connectors/  Read-only Qdrant and pgvector connectors, and the
               NDJSON snapshot format/connector.
  reconcile/   Matching, the finding taxonomy, the reconciliation
               engine (small-data and big-data paths), policy v1
               evaluation, and read-only remediation planning.
  reports/     Self-contained JSON/HTML report rendering for a
               manifest, a snapshot, and a reconciliation result.
  cli/         The `ragledger` command-line entry point.
```

Everything above runs as a local CLI process or library import. There
is no server, database, or authentication layer in this release; see
[Limitations](#limitations).

## Determinism and security notes

- **Determinism**: `build_pipeline` with `reproducible=True` (the
  `ragledger.yml` default) produces byte-identical canonical manifest
  bytes across repeated runs of the same source tree and config --
  verified by `tests/pipeline/test_build.py::test_determinism_two_runs_are_byte_identical`
  and `tests/cli/test_build.py::test_build_twice_with_the_same_epoch_is_byte_identical`.
- **Sandboxed parsers**: every document parse runs inside a subprocess
  with a timeout and an output-size cap; a hanging, crashing, or
  oversized-output parser degrades to a recorded failure, never a raised
  exception or a hung process (`ragledger.pipeline.parsers.sandbox`).
- **Read-only connectors**: the `VectorTargetConnector` interface has no
  mutation method at all. Qdrant is additionally guarded at the
  transport layer (an `httpx` request allowlist rejecting anything but
  the specific `GET`/scroll `POST` this tool issues); pgvector is
  guarded both by a SQL statement allowlist (`SELECT`/`SHOW` only) and a
  database-level read-only transaction.
- **Masked PII and ACL evidence**: no finding, report, or log ever
  carries a raw PII value or a raw ACL principal identifier. PII
  evidence is entity type, confidence, and an already-masked preview;
  ACL principals in reconciliation findings are HMAC- or SHA-256-hashed
  before they are ever attached to a finding. Both are covered by
  dedicated canary tests that build real synthetic PII/ACL data and
  assert the raw values never appear in any JSON, HTML, or CLI stdout
  output.
- **Ed25519 manifest signing**: `ragledger manifest sign`/`verify`
  attach and check detached-key Ed25519 signatures; a signature's
  `key_id` is the public key's fingerprint, never the private key
  itself, and verification distinguishes a tampered manifest from one
  signed by a merely-untrusted key.

## Limitations

Read this section before relying on this release for anything
production-sensitive.

- **The reference embedder is not a semantic model.** The embedding
  vectors this release actually produces
  (`DeterministicLocalEmbeddingProvider`) are a seeded hash projection:
  stable, content-addressed, and useful for exercising the manifest and
  reconciliation machinery, but carrying no learned semantic meaning.
  They are not suitable for real retrieval quality. Live provider
  embedders (Sentence Transformers, OpenAI) exist as config-plumbing-only
  stubs that raise an explicit "not available" error rather than a fake
  response; they are not wired to a real model call in this release.
- **Index bindings are not produced by `build`.** `ragledger build`
  always writes an empty `index_bindings` list -- the mapping from a
  manifest's embeddings to a specific target's point ids is a separate
  concern (connector/target configuration or ingestion-time
  instrumentation) not yet wired into this release's CLI. In practice
  this means a manifest fresh out of `ragledger build` alone has
  nothing for `reconcile` to call "expected" yet; every observed point
  reconciles as missing evidence, not as a matched or stale one, until
  bindings exist (see `tests/reconcile/builders.py` for how a populated
  manifest is constructed for the reconciliation engine's own test
  suite, which is fully implemented and tested against exactly that
  shape).
- **No server, API, web UI, or authentication.** This release is a
  standalone CLI and Python library only. Workspaces, roles, API
  tokens, a hosted lineage explorer, and SSE progress/audit trails are
  specified but not part of v0.1.0.
- **Docling and Presidio are not integrated.** PDF/HTML/text parsing
  uses this project's own native deterministic adapters, not Docling;
  DOCX is not supported. PII detection uses deterministic regex/checksum
  recognizers, not Presidio's NLP-based analyzer.
- **Live integration tests require Docker.** `tests/connectors/test_integration.py`
  exercises the Qdrant/pgvector connectors against real services started
  by `docker-compose.yml`; these are skipped by default
  (`RAGLEDGER_IT=1` opts in) and were not run to produce this release
  (see `docs/reviews/v0.1-security.md`).
- **Not a general RAG platform.** This tool does not parse queries,
  perform retrieval, generate text, or manage a vector index's schema.
  It is a lineage manifest format plus a read-only reconciliation
  engine for the index a separate RAG pipeline already builds and
  serves.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow,
code style, and test requirements.

## Security

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## License

Apache License 2.0. See [LICENSE](LICENSE).
