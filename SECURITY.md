# Security Policy

## Reporting a vulnerability

If you believe you have found a security vulnerability in RAG Knowledge
Ledger, report it through GitHub private security advisories for this
repository (repository "Security" tab, "Report a vulnerability"). Do not
open a public GitHub issue for a suspected vulnerability.

Please include, to the extent you can:

- The affected component (for example: manifest signing, a specific
  connector, the pipeline sandbox, the API).
- Steps to reproduce, including relevant configuration.
- The potential impact as you understand it.
- Any suggested remediation, if you have one.

## Supported versions

This project has not yet reached a 1.0 release. Until a first stable
release is published, security fixes are made against the `main` branch
only. Once tagged releases exist, this section will list which release
lines receive security fixes.

## Scope and known risk areas

Given the nature of this project, the following areas are treated as
higher risk and are of particular interest for reports:

- Manifest canonicalization, hashing, and Ed25519 signing/verification.
- Parsing of untrusted source documents (PDF, DOCX, HTML, and other
  supported formats) and the parser sandbox boundary.
- Connector credential handling and read-only enforcement against Qdrant
  and pgvector targets.
- Server-side request forgery (SSRF) protections on user-configured
  target endpoints.
- Handling of PII findings, license assertions, and ACL/tenant metadata,
  including any path by which raw PII values or credentials could reach
  logs, exports, or reports.

Connectors are read-only by design: they must never issue a write,
delete, or schema-mutating request against a configured target. A
report describing a way to make a connector mutate a target is treated
as a security issue even if no data is lost.

This project does not perform automatic remediation (index writes or
deletes) against any target. Reports assuming such automatic
remediation exists are out of scope, since v1 intentionally only
produces read-only remediation plans.

## Disclosure process

Reports submitted through GitHub private security advisories are
acknowledged, investigated, and, once a fix is available, disclosed
through a published security advisory and a corresponding release. We
ask reporters to allow time for a fix to be developed and released
before any public disclosure.
