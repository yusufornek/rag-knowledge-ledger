# Threat model

This document derives a threat model from the security, governance, and
integrity requirements in `PROJECT_SPEC.md` sections 11 (signing and
integrity), 12 (PII, license, and governance), and 19 (security). It
covers the full specified system (including the M7/M8 server and web UI
components deferred past v0.1.0 per
`docs/architecture/adr/0003-v0.1-release-scope.md`), since the trust
boundaries and data sensitivity are defined by the specification
regardless of which milestone currently implements them. This document
is updated as components are implemented and as new components are
designed.

## Untrusted inputs

The following inputs are explicitly untrusted data, never instructions,
anywhere in the system:

- **Document content**: the bytes of any source document (PDF, DOCX,
  HTML, Markdown, plain text) discovered under a configured source
  root. Documents may be adversarially crafted to exploit parser
  vulnerabilities, attempt path/zip traversal, embed macros or active
  content, or contain text designed to look like instructions to a
  downstream LLM.
- **Chunk text**: text produced by chunking parsed documents, including
  contextualized chunk text. Chunk text inherits all document-content
  risk and additionally may be used as embedding input; it is never
  treated as configuration or as an instruction to any part of the
  system.
- **Vector payloads**: metadata payloads observed from a connected
  vector index (Qdrant point payloads, pgvector row columns). These are
  produced by whatever ingestion process wrote the target index, which
  may not be this project, and are not trusted to be well-formed,
  internally consistent, or non-malicious (for example, a payload field
  could contain script-like content intended for a UI that renders it,
  or values crafted to break naive metadata comparisons).

No document content, chunk text, or vector payload value is ever passed
to a code execution path, a shell command, or an LLM acting as a
decision-maker; the deterministic core (manifest building and
reconciliation) never invokes an LLM at all.

## Assets

| Asset | Description |
|---|---|
| Source documents | Original bytes discovered from a configured source root; may contain PII, licensed content, or confidential material. |
| Parsed/chunk artifacts | Canonical JSON representations of parsed documents and chunk text derived from source documents; inherit source sensitivity. |
| Manifests | Signed, content-addressed lineage records; integrity-critical, and may reference sensitive locators/hashes. |
| Signing keys | Ed25519 private keys used to sign manifests; compromise allows forged "trusted" manifests. |
| Target credentials | Credentials used to read Qdrant/pgvector/other targets; compromise allows reading (or, if misused, attempting to write) a customer's live index or database. |
| PII findings | Entity type, confidence, and masked/HMAC evidence about detected PII; must never carry raw PII values. |
| License and ACL/tenant assertions | Governance facts about what a source may be used for and who may access it; incorrect assertions can cause data leakage or license violation downstream. |
| Vector index inventory (snapshots) | Observed state of a customer's vector index; sensitive because it reflects real production data shape and, if payloads are retained, real payload content. |
| Audit trail / logs | Record of who did what; must not itself leak secrets or raw PII. |

## Trust boundaries

```text
+-------------------------------------------------------------+
| Untrusted: source documents, chunk text, vector payloads     |
+-------------------------------------------------------------+
                |                          |
                v                          v
   +------------------------+   +----------------------------+
   | Parser sandbox         |   | Read-only connectors        |
   | (Docling/native, per   |   | (Qdrant, pgvector, NDJSON)  |
   | project spec 19.3:     |   | - no mutation API/SQL       |
   | non-root, read-only    |   | - read-only DB role/API key |
   | root fs, no network,   |   | - SSRF-validated endpoints  |
   | resource-limited OCI)  |   |                              |
   +------------------------+   +----------------------------+
                |                          |
                v                          v
   +-------------------------------------------------------------+
   | Trusted core: deterministic manifest/reconciliation engine   |
   | (canonicalization, identity, signing, policy evaluation)     |
   +-------------------------------------------------------------+
                |
                v
   +-------------------------------------------------------------+
   | Artifact store (parsed docs, chunk text, snapshots, reports) |
   | - content-addressed, sensitivity-labeled                     |
   | - signing keys and target credentials never stored here      |
   +-------------------------------------------------------------+
```

- **Parser sandbox boundary**: everything that touches raw document
  bytes runs in an isolated worker (per specification 19.3: non-root,
  read-only root filesystem, no network access, input mounted read-only
  and output read-write, dropped capabilities, and CPU/RAM/PID/page/file
  limits). The API/CLI process that orchestrates a build never parses
  a raw document itself. Output crossing this boundary is a structured
  `LedgerDocument` / `ParseResult`, not arbitrary code or an active
  document format.
- **Read-only connector boundary**: any code path that talks to a
  configured target (Qdrant, pgvector) is restricted, at both the
  interface level and the credential level (read-only API key / DB
  role with `SELECT` only), to non-mutating operations. This boundary
  is defense in depth: it must hold even if application-level checks
  are bypassed, because the underlying credential itself lacks write
  privilege.
- **Artifact store boundary**: parsed artifacts, chunk text artifacts,
  snapshots, and reports are stored with a sensitivity label and a
  content hash. Signing keys and target credentials are never written
  into this store; they live in dedicated secret storage (local
  encrypted key file / secret-mounted file for signing keys, encrypted
  credential storage for target credentials once M7 persistence
  exists).

## STRIDE threats and mitigations

| Category | Threat | Affected asset(s) | Mitigation |
|---|---|---|---|
| Spoofing | Manifest presented as signed by a trusted party when it is not, or signed with a key not actually controlled by the claimed signer | Manifests, signing keys | Ed25519 signature verification against an explicit local trust store keyed by public-key fingerprint; unknown keys are cryptographically valid but reported `untrusted`, never silently trusted (spec 19.5) |
| Spoofing | Connector impersonates a legitimate target or is pointed at an unintended internal service (SSRF) | Target credentials, internal network | Scheme/host/DNS validation, CIDR allowlist, redirects disabled by default; private targets require explicit `ALLOW_PRIVATE_TARGETS` and an exact CIDR/host allowlist (spec 19.2, 20) |
| Tampering | Manifest content modified after creation without detection | Manifests | RFC 8785 canonicalization, SHA-256 `manifest_hash`, Ed25519 signature over a fixed domain-separated digest; any field change invalidates the signature (spec 11.1) |
| Tampering | Malicious document exploits a parser vulnerability to modify the host or escape the parser process | Source documents, host/worker | Sandboxed OCI parser worker: non-root, read-only root fs, no network, dropped capabilities, resource limits (spec 19.3) |
| Tampering | Connector attempts (via bug or malicious config) to write to a target instead of only reading | Target data integrity | Interface-level read-only contract plus credential-level enforcement (read-only API key / `SELECT`-only DB role); tests assert no mutating call is possible at compile/type level where feasible and verified at runtime as defense in depth (spec 19.2, 19.4, 42.2) |
| Tampering | Archive-based import (NDJSON/portable export bundles) writes outside the intended extraction directory (zip/path traversal) | Artifact store, host filesystem | Archive path bounds validation, rejection of traversal sequences and symlink escapes on import (spec 19.2) |
| Repudiation | No record of who ran a build, signed a manifest, or configured a target | Audit trail | Audit trail records administrative and sensitive-reveal actions (raw artifact download, target configuration changes) once the M7 persistence layer exists; CLI-only v0.1.0 relies on local shell/CI logs and signed manifests as the evidence trail |
| Information disclosure | Raw PII values leak into findings, database rows, logs, or exports | PII findings | Findings store only entity type, confidence, offsets, masked preview, and HMAC (workspace-scoped key via HKDF); raw values are never persisted; canary tests scan all report/log/export surfaces except the intentionally raw source artifact (spec 12.1, 42.3) |
| Information disclosure | ACL/tenant metadata or principal identifiers leak in a public export | ACL/tenant assertions | Redaction/hashing policy applied on public export; principal identifiers normalized but only exposed per workspace policy (spec 12.3, FR-071) |
| Information disclosure | Target credentials leak via logs, error messages, or export | Target credentials | AES-256-GCM at-rest encryption, write-only storage, secret redaction in config/log dumps, credentials never included in manifest/workspace export (spec 19.2, 41) |
| Information disclosure | Signing key leaks via UI upload path, log, or manifest embedding | Signing keys | No web UI private-key upload/storage; key supplied only via CLI-encrypted local file or secret-mounted file for CI/server; 0600 file mode; key material never logged or embedded in a manifest (spec 11.2, 19.2) |
| Information disclosure | Stored XSS via document text rendered in a future web UI | End users of the (future) web UI | Document-derived text is escaped/sanitized on render; document HTML is never rendered as active content (spec 19.2) |
| Denial of service | Malicious document (zip bomb, oversized PDF, pathological structure) exhausts parser worker resources | Parser workers, availability | File size caps (100 MiB default), PDF page caps (500 default), sandbox CPU/RAM/PID/file limits, archive bounds checks (spec 19.2, 19.3, FR-014) |
| Denial of service | Reconciliation over a very large (1M+ point) inventory exhausts process memory | Reconciliation engine, availability | Streaming hash-join / external merge design bounded to roughly 1 GiB at 1M points, rather than loading full inventories into memory (spec 3.2, FR-121) |
| Denial of service | Unbounded/slow custom PII recognizer regex causes catastrophic backtracking | PII scanning, availability | Custom recognizers are bounded and timeout-protected (spec FR-054) |
| Elevation of privilege | A workspace member or API caller accesses another workspace's data (IDOR) | Manifests, findings, snapshots | Scoped repositories and explicit cross-workspace authorization tests once the M7 API/workspace layer exists (spec 19.2) |
| Elevation of privilege | pgvector connector configuration allows SQL injection via user-supplied filter values | Target database | No raw SQL execution path; SQLAlchemy-quoted identifiers; `where` clauses restricted to configured allowed columns with parameterized equality/IN only, never raw fragments (spec 19.2, 35.2, FR-111) |

## Notes on current implementation status

As of M0, only the manifest v1 and policy v1 JSON Schemas exist as
design artifacts (`docs/spec/`); no parser sandbox, connector, signing,
or persistence code exists yet. This document describes the target
threat model that subsequent milestones implement against, and is
expected to gain concrete evidence references (tests, code paths) as
each mitigation is built, per the milestone review format in
`PROJECT_SPEC.md` section 43.4.
