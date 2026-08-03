"""SSRF-safe vector target URL validation, per FR-004 and PROJECT_SPEC.md section 20.

`validate_target_url` is the single gate every endpoint URL a caller
supplies for a `VectorTarget` must pass before the URL is stored or a
connector ever dials it. Section 20 ("Private targets"): public cloud
endpoints are allowed by default; private addresses require both
`ALLOW_PRIVATE_TARGETS=true` and membership in an explicit
`PRIVATE_TARGET_CIDRS` allowlist; link-local (which includes the cloud
metadata endpoint 169.254.169.254), loopback, and other special-purpose
ranges are always blocked, allowlisted or not. DNS is resolved here, at
validation time, and *every* resolved address must pass -- a hostname
that resolves to one public and one private address is rejected, since
a connector reconnecting later could be steered to the private one.

Resolution is injectable (``resolver=``) so tests never depend on real
DNS; the default resolver is `socket.getaddrinfo`.

This module validates and classifies; it never opens a connection.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from urllib.parse import urlsplit

from ragledger.server.settings import Settings

__all__ = [
    "TargetUrlNotAllowedError",
    "TargetUrlValidation",
    "validate_target_url",
]

_ALLOWED_SCHEMES = frozenset({"http", "https", "postgresql", "postgres"})

Resolver = Callable[[str], list[str]]


class TargetUrlNotAllowedError(ValueError):
    """Raised when a candidate target URL fails FR-004 validation.

    The message never echoes credentials embedded in the URL; it names
    the host and the rule that rejected it.
    """


@dataclass(frozen=True)
class TargetUrlValidation:
    """The outcome of a successful validation, for storage/audit.

    ``decision`` is what `VectorTarget.allowlist_decision` records:
    ``"public"`` for a URL whose every resolved address is public, or
    ``"private_cidr_allowlisted"`` when private addresses were accepted
    under `ALLOW_PRIVATE_TARGETS` plus `PRIVATE_TARGET_CIDRS`.
    ``endpoint_redacted`` is the display-safe ``scheme://host[:port]``
    summary (userinfo, path, and query stripped) that
    `VectorTarget.endpoint_redacted` stores.
    """

    decision: str
    endpoint_redacted: str
    resolved_addresses: tuple[str, ...]


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def _is_always_blocked(address: IPv4Address | IPv6Address) -> str | None:
    """Return the rule name that unconditionally blocks ``address``, or `None`.

    These categories are blocked even under `ALLOW_PRIVATE_TARGETS`:
    section 20 says "Link-local/cloud metadata blocked" with no
    allowlist escape hatch, and the others are never legitimate
    connector destinations.
    """
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast:
        return "multicast"
    if address.is_unspecified:
        return "unspecified"
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return _is_always_blocked(address.ipv4_mapped)
    return None


def _is_private(address: IPv4Address | IPv6Address) -> bool:
    """Anything not globally routable counts as private for FR-004.

    `is_global` (the IANA special-purpose registries) is the deciding
    predicate rather than `is_private` alone: RFC 1918/ULA space,
    reserved space, and documentation ranges are all unreachable from
    the public internet, so a target claiming to live there is either
    a misconfiguration or an SSRF probe -- both belong behind the
    explicit `PRIVATE_TARGET_CIDRS` allowlist.
    """
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return _is_private(address.ipv4_mapped)
    return not address.is_global


def _allowlisted(address: IPv4Address | IPv6Address, settings: Settings) -> bool:
    for entry in settings.private_target_cidrs.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if address in ip_network(entry, strict=False):
            return True
    return False


def validate_target_url(
    url: str,
    *,
    settings: Settings,
    resolver: Resolver | None = None,
) -> TargetUrlValidation:
    """Validate ``url`` as a vector target endpoint, per FR-004.

    Raises `TargetUrlNotAllowedError` on any failure; returns a
    `TargetUrlValidation` describing why the URL was accepted otherwise.
    """
    split = urlsplit(url)
    if split.scheme not in _ALLOWED_SCHEMES:
        raise TargetUrlNotAllowedError(
            f"target URL scheme {split.scheme!r} is not allowed; "
            f"expected one of {sorted(_ALLOWED_SCHEMES)}"
        )
    host = split.hostname
    if not host:
        raise TargetUrlNotAllowedError("target URL has no host")

    try:
        literal: IPv4Address | IPv6Address | None = ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        resolved = [str(literal)]
    else:
        resolve = resolver if resolver is not None else _default_resolver
        try:
            resolved = resolve(host)
        except OSError as exc:
            raise TargetUrlNotAllowedError(f"target host {host!r} did not resolve") from exc
        if not resolved:
            raise TargetUrlNotAllowedError(f"target host {host!r} did not resolve")

    any_private = False
    for raw in resolved:
        address = ip_address(raw)
        blocked_rule = _is_always_blocked(address)
        if blocked_rule is not None:
            raise TargetUrlNotAllowedError(
                f"target host {host!r} resolves to {raw}, which is always blocked ({blocked_rule})"
            )
        if _is_private(address):
            any_private = True
            if not settings.allow_private_targets:
                raise TargetUrlNotAllowedError(
                    f"target host {host!r} resolves to private address {raw} "
                    "and ALLOW_PRIVATE_TARGETS is not enabled"
                )
            if not _allowlisted(address, settings):
                raise TargetUrlNotAllowedError(
                    f"target host {host!r} resolves to private address {raw}, "
                    "which is not in PRIVATE_TARGET_CIDRS"
                )

    port = f":{split.port}" if split.port is not None else ""
    redacted = f"{split.scheme}://{host}{port}"
    decision = "private_cidr_allowlisted" if any_private else "public"
    return TargetUrlValidation(
        decision=decision,
        endpoint_redacted=redacted,
        resolved_addresses=tuple(resolved),
    )
