"""
egress_broker.py - the single rule and primitive for all outbound traffic.

Every SSRF defense before this was enumerate-badness (a hand-maintained list of
blocked CIDRs) and resolve-then-discard: the validator resolved a hostname,
checked the IP against the list, then threw the IP away and let the real
connection re-resolve (DNS rebinding / TOCTOU). It also missed whole families:
the IPv6 unspecified ::, NAT64 64:ff9b::/96, 6to4 2002::/16, and
host.docker.internal as anything but a literal prefix.

This module replaces the blocklist with one rule:

    reject any resolved IP where `not ip.is_global`

evaluated on every transition-embedded IPv4 form as well (v4-mapped, NAT64,
6to4, v4-compatible), and makes the entity that resolves DNS the same entity
that hands back the pinned IP to dial. `resolve_and_pin()` resolves once and
returns the exact IP to connect to; callers must dial that IP (Host/SNI
preserved) so a second, attacker-controlled resolution is never used.

Errors are opaque (`EgressBlocked.reason == "URL blocked"`) so the API never
leaks a resolved internal IP, hostname, or traceback (the old DNS oracle).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

# Operator escape hatch for trusted internal deployments (off by default).
ALLOW_INTERNAL = os.environ.get("CRAWL4AI_ALLOW_INTERNAL_URLS", "false").lower() == "true"

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4COMPAT = ipaddress.ip_network("::/96")
_6TO4 = ipaddress.ip_network("2002::/16")

logger = logging.getLogger(__name__)

# Hostnames we refuse regardless of resolution (belt-and-suspenders; they also
# resolve to non-global addresses and would be caught anyway).
_BLOCKED_HOSTNAMES = {
    "localhost", "metadata.google.internal", "metadata",
    "kubernetes.default", "kubernetes.default.svc",
}


class EgressBlocked(Exception):
    """Outbound target rejected. Carries an OPAQUE reason - never an IP/host."""

    def __init__(self, reason: str = "URL blocked"):
        self.reason = reason
        super().__init__(reason)


# The opaque `reason` above is what the CALLER sees, and it must stay opaque:
# distinguishing "did not resolve" from "resolved to a forbidden IP" would let a
# caller probe for internal hostnames. The server log has no such constraint,
# and without it a block is undiagnosable - a healthy public domain rejected
# once in a few hundred lookups is indistinguishable from a policy decision,
# which cost a long investigation with no way to tell the two apart.
#
# Record the deciding branch here only. Never surface `cause` to a caller.
def _log_block(cause: str, host: str = "", detail: str = "") -> "EgressBlocked":
    logger.info(
        "egress blocked: cause=%s host=%s%s",
        cause, host or "?", f" detail={detail}" if detail else "",
    )
    return EgressBlocked()


@dataclass
class PinnedTarget:
    scheme: str
    host: str          # original hostname (for Host header / SNI)
    port: int
    ip: str            # the exact IP to dial - do NOT re-resolve `host`


def _embedded_v4_forms(ip: ipaddress._BaseAddress) -> List[ipaddress._BaseAddress]:
    """The address plus any IPv4 embedded in a transition IPv6 form.

    Only the well-defined transition ranges are unwrapped, so a normal global
    IPv6 is never mis-derived into a bogus (and possibly non-global) IPv4.
    """
    forms: List[ipaddress._BaseAddress] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            forms.append(mapped)
        elif ip in _NAT64 or ip in _V4COMPAT:
            forms.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        elif ip in _6TO4:
            forms.append(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF))
    return forms


def is_forbidden_ip(ip_str: str) -> bool:
    """True if the IP (or any embedded transition form) is not globally routable."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return any(not form.is_global for form in _embedded_v4_forms(ip))


# A dropped lookup is not a policy decision, but it reaches the caller as the
# same opaque EgressBlocked -> HTTP 400 "URL blocked (SSRF protection)" that a
# genuinely forbidden target does. 400 is terminal for clients (a 5xx would be
# retried), so one momentary resolver hiccup permanently failed that URL and
# blamed it on security policy.
#
# This is observed, not theoretical. The container resolves through Docker's
# embedded DNS (127.0.0.11), which drops lookups when a batch crawl runs many
# concurrently. Instrumentation caught it live: be-modaco.com and
# bearwoodconcepts.com were both rejected with
# "cause=dns_failure ... [Errno -5] No address associated with hostname", and
# both resolved normally seconds later - one had crawled successfully minutes
# before.
#
# The status cannot be split by cause without building a DNS oracle: separate
# statuses for "did not resolve" and "resolved to a forbidden IP" would tell a
# caller whether an internal hostname exists, which is what EgressBlocked's
# opaque reason exists to prevent. So absorb the transience here instead. A
# host that genuinely does not resolve still fails after the retries, and
# terminal is the correct answer for it.
#
# Bounded and short: this sits in the request path, and the added delay is paid
# only by hosts that fail to resolve at all.
_RESOLVE_ATTEMPTS = 3
_RESOLVE_BACKOFF_S = 0.05


def _resolve(host: str, port: int):
    for attempt in range(_RESOLVE_ATTEMPTS):
        try:
            answers = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            if attempt:
                # Log recoveries so the retry is measurable. Without this, a run
                # with no dns_failure lines is indistinguishable from a run where
                # DNS never faltered, and there is no way to show this works.
                logger.info(
                    "dns_retry recovered: host=%s attempt=%d", host, attempt + 1
                )
            return answers
        except socket.gaierror as e:
            if attempt == _RESOLVE_ATTEMPTS - 1:
                # Only a give-up counts as dns_failure, so that counter keeps
                # meaning "a lookup was abandoned".
                raise _log_block("dns_failure", host, str(e))
            time.sleep(_RESOLVE_BACKOFF_S * (2 ** attempt))
    raise _log_block("dns_failure", host)  # unreachable; keeps the exit explicit


def assert_host_allowed(host: str, port: int = 0) -> None:
    """Resolve `host` and reject if ANY answer is non-global. Opaque on failure."""
    if ALLOW_INTERNAL:
        return
    if not host:
        raise EgressBlocked()
    low = host.lower()
    if low in _BLOCKED_HOSTNAMES or low.startswith("host.docker.internal"):
        raise EgressBlocked()
    for *_, sockaddr in _resolve(host, port):
        if is_forbidden_ip(sockaddr[0]):
            raise EgressBlocked()


def resolve_and_pin(url: str) -> PinnedTarget:
    """Resolve `url` once, reject if any answer is non-global, and pin one IP.

    The returned PinnedTarget.ip is the address the caller must dial; resolving
    `host` again at connect time would reopen the rebinding hole.
    """
    parsed = urlparse(str(url))
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise EgressBlocked()
    host = parsed.hostname
    if not host:
        raise EgressBlocked()
    port = parsed.port or (443 if scheme == "https" else 80)

    if ALLOW_INTERNAL:
        # Still resolve so we can pin, but skip the global check.
        answers = _resolve(host, port)
        return PinnedTarget(scheme, host, port, answers[0][4][0])

    low = host.lower()
    if low in _BLOCKED_HOSTNAMES or low.startswith("host.docker.internal"):
        raise _log_block("blocked_hostname", host)

    answers = _resolve(host, port)
    pinned = None
    for *_, sockaddr in answers:
        if is_forbidden_ip(sockaddr[0]):
            # Reject the host outright if ANY of its records is internal.
            raise _log_block("forbidden_ip", host, sockaddr[0])
        if pinned is None:
            pinned = sockaddr[0]
    if pinned is None:
        raise _log_block("empty_answers", host)
    return PinnedTarget(scheme, host, port, pinned)


def check_redirect(location: str) -> PinnedTarget:
    """Re-validate (and pin) a redirect Location. Same rule as the initial hop."""
    return resolve_and_pin(location)


ALLOW_INSECURE_TLS = os.environ.get("CRAWL4AI_ALLOW_INSECURE_TLS", "false").lower() == "true"

# URL of the localhost pinning forward-proxy (egress_proxy.py), set at boot.
# When present, enforce_egress routes the browser through it so Chromium never
# resolves the target itself (closes DNS rebinding on the browser path).
_EGRESS_PROXY_URL: Optional[str] = None


def set_egress_proxy(url: Optional[str]) -> None:
    global _EGRESS_PROXY_URL
    _EGRESS_PROXY_URL = url


def get_egress_proxy() -> Optional[str]:
    return _EGRESS_PROXY_URL

# Chromium flags that would re-route or weaken egress; scrubbed server-side.
_DANGEROUS_BROWSER_ARGS = (
    "--proxy-server", "--proxy-pac-url", "--proxy-bypass-list",
    "--host-resolver-rules", "--ignore-certificate-errors",
    "--allow-insecure-localhost",
)


def enforce_egress(browser_config) -> None:
    """Server-side egress hardening applied to the effective browser config.

    R2 already forbids untrusted bodies from setting proxy/extra_args, so this
    is defense in depth that also covers server/SDK-built configs:
      - TLS verification ON (ignore_https_errors=False) unless the operator
        opts into CRAWL4AI_ALLOW_INSECURE_TLS;
      - no caller proxy (the key/SSRF redirect vector);
      - strip any proxy/TLS-weakening Chromium launch flags.
    """
    if browser_config is None:
        return
    if not ALLOW_INSECURE_TLS and hasattr(browser_config, "ignore_https_errors"):
        browser_config.ignore_https_errors = False
    # Drop any caller proxy, then route the browser through the pinning proxy so
    # Chromium never resolves the target host itself (DNS-rebinding control).
    for attr in ("proxy", "proxy_config"):
        if getattr(browser_config, attr, None) is not None:
            setattr(browser_config, attr, None)
    if _EGRESS_PROXY_URL and hasattr(browser_config, "proxy_config"):
        try:
            from crawl4ai import ProxyConfig
            browser_config.proxy_config = ProxyConfig(server=_EGRESS_PROXY_URL)
        except Exception:
            pass
    args = getattr(browser_config, "extra_args", None)
    if args:
        browser_config.extra_args = [
            a for a in args
            if not any(str(a).startswith(bad) for bad in _DANGEROUS_BROWSER_ARGS)
        ]
