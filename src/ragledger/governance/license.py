"""SPDX license detection and assertion, per PROJECT_SPEC.md section 12.2 and FR-060..FR-064.

Sources evaluated, per FR-060: an explicit user assertion (an
operator-supplied override keyed by source URI), a sidecar metadata
file, Markdown frontmatter (including an explicit
`SPDX-License-Identifier:` header, treated the same way -- a
structurally explicit declaration at a fixed convention location, not a
prose guess), a path rule (glob pattern -> SPDX expression), and a
configured repository default. **Content-text guessing is never a v1
fact** (FR-060: "content text tahmini v1 fact değildir") -- this module
never scans document body prose looking for phrases like "licensed
under MIT".

Precedence: PROJECT_SPEC.md section 12.2 states explicitly "sidecar >
frontmatter > path policy > repository default > NOASSERTION" but does
not place `user_assertion` (present as FR-060's first-listed source and
as `LicenseMethod`'s first enum member) in that chain. This module
ranks `user_assertion` above `sidecar`: an explicit, operator-supplied
override is the most direct, highest-trust signal available. This is a
documented interpretation, not a literal transcription of the spec
text -- see `_PRECEDENCE`.

Conflicts are never silently resolved by precedence (section 12.2:
"Conflict hiçbir zaman precedence ile sessiz çözülmez"): when two or
more applicable sources disagree on the resolved SPDX expression, every
resulting `LicenseAssertion` is kept and cross-references every other
one via `conflicting_assertion_ids`; only one (the highest-precedence)
is marked as `effective` by `evaluate_license`'s return value.

An SPDX expression's every referenced license identifier is validated
against `_KNOWN_SPDX_IDENTIFIERS`; anything unrecognized becomes
`NOASSERTION` (FR-061) rather than being partially accepted or guessed.
`_KNOWN_SPDX_IDENTIFIERS`/`_LICENSE_LIST_VERSION` are an honest, small,
hand-maintained subset -- not the real, network-fetched official SPDX
license list -- see `_LICENSE_LIST_VERSION`'s docstring and
`IMPLEMENTATION_STATUS.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch

import yaml

from ragledger.core.models import LicenseAssertion, LicenseMethod
from ragledger.governance.identity import derive_assertion_id

NOASSERTION = "NOASSERTION"

_LICENSE_LIST_VERSION = "ragledger-embedded-subset-1"
"""Not the official SPDX license-list-data release version: this
environment has no network access to fetch or pin the real upstream
list. This is ragledger's own identifier for the small, hand-maintained
identifier subset in `_KNOWN_SPDX_IDENTIFIERS`, reported honestly as
what it is (PROJECT_SPEC.md section 0 rule 2: never fabricate a fact).
Vendoring the full official list is a documented gap.
"""

_KNOWN_SPDX_IDENTIFIERS = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MPL-2.0",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "Unlicense",
        "0BSD",
    }
)

_EXPRESSION_TOKEN_RE = re.compile(r"\(|\)|AND|OR|WITH|[A-Za-z0-9.\-+]+")
_SPDX_HEADER_RE = re.compile(r"SPDX-License-Identifier:\s*(.+)", re.IGNORECASE)

_PRECEDENCE: dict[LicenseMethod, int] = {
    "user_assertion": 0,
    "sidecar": 1,
    "frontmatter": 2,
    "path_rule": 3,
    "repository_default": 4,
}


def _tokenize_expression(expression: str) -> list[str]:
    return _EXPRESSION_TOKEN_RE.findall(expression)


def validate_spdx_expression(expression: str) -> bool:
    """Validate an SPDX license expression's syntax and every referenced identifier.

    Supports `AND`/`OR`/`WITH`, parentheses, and the trailing `+`
    "or-later" suffix. Every leaf license identifier (ignoring a
    trailing `+`) must be present in `_KNOWN_SPDX_IDENTIFIERS`, or the
    whole expression is rejected -- there is no partial acceptance.
    """
    stripped = expression.strip()
    if stripped == NOASSERTION:
        return True
    tokens = _tokenize_expression(stripped)
    if not tokens:
        return False
    depth = 0
    expect_operand = True
    for token in tokens:
        if token == "(":
            if not expect_operand:
                return False
            depth += 1
            continue
        if token == ")":
            if expect_operand or depth == 0:
                return False
            depth -= 1
            continue
        if token in ("AND", "OR", "WITH"):
            if expect_operand:
                return False
            expect_operand = True
            continue
        if not expect_operand:
            return False
        base_id = token[:-1] if token.endswith("+") else token
        if base_id not in _KNOWN_SPDX_IDENTIFIERS:
            return False
        expect_operand = False
    return depth == 0 and not expect_operand


def detect_spdx_header(text: str, max_lines: int = 20) -> str | None:
    """Detect an explicit `SPDX-License-Identifier:` header near the top of a file.

    Treated as a `frontmatter`-method signal: a structurally explicit,
    unambiguous declaration at a fixed convention location, not a prose
    guess over document body text.
    """
    for line in text.splitlines()[:max_lines]:
        match = _SPDX_HEADER_RE.search(line)
        if match:
            return match.group(1).strip()
    return None


def read_sidecar_expression(sidecar_text: str) -> str | None:
    """Parse a `<source>.license` sidecar file's content.

    Accepts either a bare SPDX expression as the file's entire stripped
    content, or a structured `{"spdx_expression": "..."}` /
    `spdx_expression: ...` form (JSON or YAML -- YAML is a JSON
    superset, so `yaml.safe_load` parses both).
    """
    stripped = sidecar_text.strip()
    if not stripped:
        return None
    try:
        loaded = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return stripped
    if isinstance(loaded, dict):
        value = loaded.get("spdx_expression")
        return value.strip() if isinstance(value, str) and value.strip() else None
    if isinstance(loaded, str):
        return loaded.strip()
    return stripped


@dataclass(frozen=True)
class LicenseCandidate:
    method: LicenseMethod
    raw_expression: str
    confidence: float | None = None


@dataclass(frozen=True)
class PathRule:
    pattern: str
    spdx_expression: str


@dataclass(frozen=True)
class LicenseConfig:
    user_assertions: dict[str, str] = field(default_factory=dict)
    path_rules: tuple[PathRule, ...] = ()
    repository_default: str | None = None


def gather_candidates(
    uri: str,
    frontmatter: dict[str, object] | None,
    sidecar_expression: str | None,
    config: LicenseConfig,
    spdx_header: str | None = None,
) -> list[LicenseCandidate]:
    """Collect every applicable, unresolved license candidate for one source.

    `spdx_header` is the result of `detect_spdx_header` run over the
    source's raw text, kept as a caller-supplied parameter (rather than
    smuggled into `frontmatter`) since it is a distinct signal detected
    a different way -- a fixed-convention header line, not a parsed
    YAML frontmatter block -- even though both resolve to the same
    `"frontmatter"` `LicenseMethod` (both are explicit, in-file,
    structurally unambiguous declarations, as opposed to a prose guess).
    A declared `frontmatter["license"]` value takes priority over a
    detected header when a source happens to carry both.
    """
    candidates: list[LicenseCandidate] = []
    user_expression = config.user_assertions.get(uri)
    if user_expression:
        candidates.append(LicenseCandidate(method="user_assertion", raw_expression=user_expression))
    if sidecar_expression:
        candidates.append(LicenseCandidate(method="sidecar", raw_expression=sidecar_expression))
    declared = frontmatter.get("license") if frontmatter else None
    for value in (declared, spdx_header):
        if isinstance(value, str) and value.strip():
            candidates.append(LicenseCandidate(method="frontmatter", raw_expression=value.strip()))
            break
    for rule in config.path_rules:
        if fnmatch(uri, rule.pattern):
            candidates.append(
                LicenseCandidate(method="path_rule", raw_expression=rule.spdx_expression)
            )
            break
    if config.repository_default:
        candidates.append(
            LicenseCandidate(method="repository_default", raw_expression=config.repository_default)
        )
    return candidates


def evaluate_license(
    uri: str,
    frontmatter: dict[str, object] | None,
    sidecar_expression: str | None,
    config: LicenseConfig,
    subject_ref: str,
    created_at: datetime,
    spdx_header: str | None = None,
) -> tuple[LicenseAssertion, list[LicenseAssertion]]:
    """Resolve every applicable license source into one effective assertion (FR-060).

    Returns `(effective, all_candidates)`; `all_candidates` always
    includes `effective`. When candidates disagree on the resolved SPDX
    expression, every candidate's `conflicting_assertion_ids` lists
    every other candidate's id (FR-062) -- conflicts are never silently
    dropped, only the single highest-precedence one becomes `effective`.
    """
    raw_candidates = gather_candidates(uri, frontmatter, sidecar_expression, config, spdx_header)
    if not raw_candidates:
        raw_candidates = [LicenseCandidate(method="repository_default", raw_expression=NOASSERTION)]

    assertions: list[LicenseAssertion] = []
    for candidate in raw_candidates:
        expression = (
            candidate.raw_expression
            if validate_spdx_expression(candidate.raw_expression)
            else NOASSERTION
        )
        assertion_id = derive_assertion_id("lic", subject_ref, candidate.method, expression)
        assertions.append(
            LicenseAssertion(
                id=assertion_id,
                subject_ref=subject_ref,
                created_at=created_at,
                spdx_expression=expression,
                method=candidate.method,
                confidence=candidate.confidence,
                license_list_version=_LICENSE_LIST_VERSION,
            )
        )

    distinct = {assertion.spdx_expression for assertion in assertions}
    if len(distinct) > 1:
        for index, assertion in enumerate(assertions):
            others = [
                other.id for other_index, other in enumerate(assertions) if other_index != index
            ]
            assertions[index] = assertion.model_copy(update={"conflicting_assertion_ids": others})

    effective = min(assertions, key=lambda assertion: _PRECEDENCE.get(assertion.method, 99))
    return effective, assertions
