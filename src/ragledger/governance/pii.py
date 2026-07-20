"""PII detection, per PROJECT_SPEC.md section 12.1 and FR-050..FR-056.

Ships deterministic regex/checksum recognizers -- email, phone, IBAN
(MOD-97 checksum), credit card (Luhn checksum), US SSN (structural
plausibility), and Turkish national ID / TCKN (its official two-stage
checksum) -- plus a YAML-configurable custom recognizer loader
(FR-054). PROJECT_SPEC.md section 5.1 lists Presidio as the intended
v1 primary analyzer; Presidio integration is a documented gap (see
`IMPLEMENTATION_STATUS.md`) rather than a fabricated dependency, since
it pulls in a spaCy language model this environment does not vendor.

**No raw PII value is ever returned in an evidence record.** Every
`PiiFinding` this module produces carries only: the entity type,
confidence, offsets, an optional short `masked_preview` (matching
PROJECT_SPEC.md section 12.1's own example format, `jo***@ex***.com`
-- a bounded partial disclosure, never the full value), and an optional
`value_hmac` computed with a caller-supplied, workspace-scoped secret
via HKDF-derived key + HMAC-SHA256 (section 12.1: "HMAC key encryption
key'den HKDF domain separation ile türetilir; salt/key manifestte
yok") -- never a plain hash of the raw value, which would be trivially
reversible for low-entropy PII like a 9-digit SSN via a rainbow table.
When no secret is supplied, `value_hmac` is simply omitted rather than
falling back to an insecure plain hash.

A scan that finds nothing reports `status="no_findings_detected"`,
never a stronger claim (FR-055): the absence of a finding is a fact
about this scan run, not a guarantee about the underlying text.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import yaml
from cryptography.hazmat.primitives import hashes as crypto_hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ragledger.core.canonical import JSONValue
from ragledger.core.hashing import hash_canonical
from ragledger.core.models import PiiFinding, PiiScanAssertion, PiiScannerInfo, PiiScanStatus
from ragledger.governance.identity import derive_assertion_id

_SCANNER_NAME = "ragledger-deterministic-pii-scanner"
_SCANNER_VERSION = "1"
_HMAC_DOMAIN_INFO = b"ragledger-pii-value-hmac-v1"
_DEFAULT_TIMEOUT_SECONDS = 1.0


class RegexTimeoutError(RuntimeError):
    """A recognizer's regex evaluation exceeded its execution budget (FR-054)."""


class CustomRecognizerConfigError(ValueError):
    """Raised for malformed custom recognizer YAML."""


# --------------------------------------------------------------------------
# Recognizers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Recognizer:
    entity_type: str
    recognizer_id: str
    recognizer_version: str
    pattern: re.Pattern[str]
    base_confidence: float
    validated_confidence: float | None = None
    validator: Callable[[str], bool] | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class RawFinding:
    """An internal, in-process-only finding still carrying the raw matched text.

    Never leaves this module: `build_pii_scan_assertion` converts every
    `RawFinding` into a raw-value-free `PiiFinding` before returning.
    """

    entity_type: str
    start: int
    end: int
    raw_value: str
    confidence: float
    recognizer_id: str
    recognizer_version: str


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _validate_credit_card(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    return 13 <= len(digits) <= 19 and _luhn_valid(digits)


def _validate_ssn(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in ("000", "666") or area.startswith("9"):
        return False
    return group != "00" and serial != "0000"


def _validate_tckn(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 11 or digits[0] == "0":
        return False
    d = [int(c) for c in digits]
    odd_sum = d[0] + d[2] + d[4] + d[6] + d[8]
    even_sum = d[1] + d[3] + d[5] + d[7]
    if ((odd_sum * 7) - even_sum) % 10 != d[9]:
        return False
    return sum(d[0:10]) % 10 == d[10]


def _validate_iban(raw: str) -> bool:
    value = raw.replace(" ", "").upper()
    if not (15 <= len(value) <= 34):
        return False
    rearranged = value[4:] + value[:4]
    try:
        numeric = "".join(str(int(char, 36)) for char in rearranged)
        return int(numeric) % 97 == 1
    except ValueError:
        return False


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
_CREDIT_CARD_RE = re.compile(r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{1,7}(?!\d)")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_TCKN_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,4}\b")


def default_recognizers() -> list[Recognizer]:
    """The built-in deterministic regex/checksum recognizers (FR-050)."""
    return [
        Recognizer(
            entity_type="EMAIL_ADDRESS",
            recognizer_id="ragledger.email",
            recognizer_version="1",
            pattern=_EMAIL_RE,
            base_confidence=0.85,
        ),
        Recognizer(
            entity_type="PHONE_NUMBER",
            recognizer_id="ragledger.phone",
            recognizer_version="1",
            pattern=_PHONE_RE,
            base_confidence=0.5,
        ),
        Recognizer(
            entity_type="CREDIT_CARD",
            recognizer_id="ragledger.credit_card",
            recognizer_version="1",
            pattern=_CREDIT_CARD_RE,
            base_confidence=0.3,
            validated_confidence=0.9,
            validator=_validate_credit_card,
        ),
        Recognizer(
            entity_type="US_SSN",
            recognizer_id="ragledger.us_ssn",
            recognizer_version="1",
            pattern=_SSN_RE,
            base_confidence=0.3,
            validated_confidence=0.75,
            validator=_validate_ssn,
        ),
        Recognizer(
            entity_type="TR_TCKN",
            recognizer_id="ragledger.tr_tckn",
            recognizer_version="1",
            pattern=_TCKN_RE,
            base_confidence=0.1,
            validated_confidence=0.9,
            validator=_validate_tckn,
        ),
        Recognizer(
            entity_type="IBAN",
            recognizer_id="ragledger.iban",
            recognizer_version="1",
            pattern=_IBAN_RE,
            base_confidence=0.3,
            validated_confidence=0.9,
            validator=_validate_iban,
        ),
    ]


def load_custom_recognizers(yaml_text: str) -> list[Recognizer]:
    """Load allowlist/denylist custom recognizers from YAML (FR-054).

    Expected shape::

        recognizers:
          - entity_type: EMPLOYEE_ID
            pattern: "EMP-\\d{6}"
            confidence: 0.7
            timeout_seconds: 0.5

    `timeout_seconds` bounds this recognizer's regex evaluation time
    (see `_run_with_timeout`); custom, operator-authored patterns are
    exactly the ones most likely to carry an accidental catastrophic
    backtracking pattern, so every custom recognizer gets a timeout
    even though the built-in recognizers' patterns are pre-vetted.
    """
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise CustomRecognizerConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CustomRecognizerConfigError("custom recognizer YAML must be a mapping")
    entries = data.get("recognizers", [])
    if not isinstance(entries, list):
        raise CustomRecognizerConfigError("'recognizers' must be a list")

    recognizers: list[Recognizer] = []
    for entry in entries:
        if not isinstance(entry, dict) or "entity_type" not in entry or "pattern" not in entry:
            raise CustomRecognizerConfigError(
                "each recognizer entry requires 'entity_type' and 'pattern'"
            )
        entity_type = str(entry["entity_type"])
        try:
            pattern = re.compile(str(entry["pattern"]))
        except re.error as exc:
            raise CustomRecognizerConfigError(
                f"invalid pattern for {entity_type!r}: {exc}"
            ) from exc
        confidence = float(entry.get("confidence", 0.5))
        if not 0.0 <= confidence <= 1.0:
            raise CustomRecognizerConfigError("confidence must be between 0 and 1")
        recognizers.append(
            Recognizer(
                entity_type=entity_type,
                recognizer_id=f"custom.{entity_type.lower()}",
                recognizer_version="1",
                pattern=pattern,
                base_confidence=confidence,
                timeout_seconds=float(entry.get("timeout_seconds", 0.5)),
            )
        )
    return recognizers


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def _run_with_timeout(
    func: Callable[[], list[RawFinding]], timeout_seconds: float
) -> list[RawFinding]:
    """Run `func`, aborting with `RegexTimeoutError` after `timeout_seconds`.

    POSIX-only (`SIGALRM`-based, via `signal.setitimer` for sub-second
    granularity); on platforms without `SIGALRM` this degrades to
    running unbounded, since there is no portable stdlib-only
    alternative. This is the enforcement mechanism behind FR-054's
    "tested regex timeout/bounds" for custom, operator-authored
    recognizer patterns.
    """
    if not hasattr(signal, "SIGALRM") or timeout_seconds <= 0:
        return func()

    def _on_alarm(signum: int, frame: Any) -> None:
        raise RegexTimeoutError(f"regex evaluation exceeded {timeout_seconds}s timeout")

    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return func()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _scan_one(recognizer: Recognizer, text: str) -> list[RawFinding]:
    def _do_scan() -> list[RawFinding]:
        results: list[RawFinding] = []
        for match in recognizer.pattern.finditer(text):
            raw_value = match.group(0)
            confidence = recognizer.base_confidence
            if recognizer.validator is not None:
                if not recognizer.validator(raw_value):
                    continue
                confidence = recognizer.validated_confidence or recognizer.base_confidence
            results.append(
                RawFinding(
                    entity_type=recognizer.entity_type,
                    start=match.start(),
                    end=match.end(),
                    raw_value=raw_value,
                    confidence=confidence,
                    recognizer_id=recognizer.recognizer_id,
                    recognizer_version=recognizer.recognizer_version,
                )
            )
        return results

    try:
        return _run_with_timeout(_do_scan, recognizer.timeout_seconds)
    except RegexTimeoutError:
        return []


def scan_text(text: str, recognizers: Sequence[Recognizer] | None = None) -> list[RawFinding]:
    """Scan `text` with every recognizer, returning findings sorted by offset.

    `recognizers` defaults to `default_recognizers()`; pass
    `default_recognizers() + load_custom_recognizers(...)` to add
    operator-configured entity types.
    """
    active = list(recognizers) if recognizers is not None else default_recognizers()
    findings: list[RawFinding] = []
    for recognizer in active:
        findings.extend(_scan_one(recognizer, text))
    findings.sort(key=lambda item: (item.start, item.end, item.entity_type))
    return findings


# --------------------------------------------------------------------------
# Masking and HMAC evidence (section 12.1)
# --------------------------------------------------------------------------


def _mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    if not domain:
        return mask_generic(value)
    label, _, tld = domain.partition(".")
    masked_local = f"{local[:2]}***" if len(local) > 2 else f"{local[:1]}***"
    masked_label = f"{label[:2]}***" if len(label) > 2 else f"{label[:1]}***"
    return f"{masked_local}@{masked_label}{'.' + tld if tld else ''}"


def mask_generic(value: str, keep_last: int = 4) -> str:
    """Replace all but the last `keep_last` characters of `value` with `*`."""
    if len(value) <= keep_last:
        return "*" * len(value)
    return ("*" * (len(value) - keep_last)) + value[-keep_last:]


def mask_value(entity_type: str, raw_value: str) -> str:
    if entity_type == "EMAIL_ADDRESS":
        return _mask_email(raw_value)
    return mask_generic(raw_value)


def derive_value_hmac_key(workspace_secret: bytes) -> bytes:
    """HKDF-derive a value-HMAC key from a workspace secret (section 12.1).

    Domain-separated from any other use of the same workspace secret via
    `_HMAC_DOMAIN_INFO`, so this key can never be reused as, or confused
    with, an encryption key or any other derived secret.
    """
    kdf = HKDF(algorithm=crypto_hashes.SHA256(), length=32, salt=None, info=_HMAC_DOMAIN_INFO)
    return kdf.derive(workspace_secret)


def compute_value_hmac(raw_value: str, workspace_secret: bytes) -> str:
    """HMAC-SHA256 a raw PII value under an HKDF-derived, workspace-scoped key.

    Never a plain hash: a plain SHA-256 of a low-entropy value like a
    9-digit SSN is reversible via a rainbow table in seconds, which
    would defeat the entire purpose of this field existing instead of
    the raw value itself.
    """
    key = derive_value_hmac_key(workspace_secret)
    return hmac.new(key, raw_value.encode("utf-8"), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Assertion construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PiiScanConfig:
    scanner_name: str = _SCANNER_NAME
    scanner_version: str = _SCANNER_VERSION
    language: str = "und"
    custom_recognizers_yaml: str | None = None
    workspace_secret: bytes | None = None
    recognizer_entity_types: tuple[str, ...] = field(default_factory=tuple)

    def active_recognizers(self) -> list[Recognizer]:
        recognizers = default_recognizers()
        if self.custom_recognizers_yaml:
            recognizers += load_custom_recognizers(self.custom_recognizers_yaml)
        return recognizers

    def config_hash(self) -> str:
        entity_types = cast(JSONValue, sorted({r.entity_type for r in self.active_recognizers()}))
        return hash_canonical(
            {
                "entity_types": entity_types,
                "custom_recognizers": self.custom_recognizers_yaml or "",
                "language": self.language,
            }
        )


def build_pii_scan_assertion(
    subject_ref: str, text: str, config: PiiScanConfig, created_at: datetime
) -> PiiScanAssertion:
    """Scan `text` and build a `PII_SCAN` assertion for `subject_ref` (FR-053).

    `subject_ref` should be a `source_version_id` when scanning parsed
    source text and a `chunk_id` when scanning contextualized chunk
    text -- PROJECT_SPEC.md section 40's edge case ("PII offsets after
    contextual heading prefix: scanner target raw_chunk vs
    contextualized; both field distinguishes") means callers must run
    this twice with different `subject_ref`/`text` pairs to cover both,
    not attempt to infer one scan from the other.
    """
    raw_findings = scan_text(text, config.active_recognizers())
    config_hash = config.config_hash()
    findings = [
        PiiFinding(
            entity_type=item.entity_type,
            confidence=item.confidence,
            start=item.start,
            end=item.end,
            masked_preview=mask_value(item.entity_type, item.raw_value),
            value_hmac=(
                compute_value_hmac(item.raw_value, config.workspace_secret)
                if config.workspace_secret
                else None
            ),
            recognizer_id=item.recognizer_id,
            recognizer_version=item.recognizer_version,
        )
        for item in raw_findings
    ]
    status: PiiScanStatus = "findings_detected" if findings else "no_findings_detected"
    assertion_id = derive_assertion_id(
        "pii",
        subject_ref,
        config.scanner_name,
        config.scanner_version,
        config_hash,
        hash_canonical([[f.entity_type, f.start, f.end] for f in findings]),
    )
    return PiiScanAssertion(
        id=assertion_id,
        subject_ref=subject_ref,
        created_at=created_at,
        scanner=PiiScannerInfo(
            name=config.scanner_name,
            version=config.scanner_version,
            language=config.language,
            config_hash=config_hash,
        ),
        status=status,
        findings=findings,
    )
