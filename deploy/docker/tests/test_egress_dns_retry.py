"""Transient DNS failures must not be reported as egress blocks.

`resolve_and_pin` calls `_resolve`, which turns any `socket.gaierror` into
`EgressBlocked`. That surfaces to the caller as HTTP 400
"URL blocked (SSRF protection)" - a *terminal* status that clients do not
retry, unlike a 5xx.

Under concurrency the container's resolver (Docker's embedded 127.0.0.11) drops
lookups, so a batch crawl sees legitimate public domains - GitHub Pages,
Shopify - reported as security blocks and permanently dropped from the run.

The status cannot be split by cause without turning the API into a DNS oracle:
distinct statuses for "did not resolve" and "resolved to a forbidden IP" would
tell an attacker whether an internal hostname exists. `EgressBlocked` is
deliberately opaque for that reason (see its docstring). So the fix is to stop
*producing* the spurious failure: retry a transient resolution error before
concluding the host is unreachable. A host that genuinely does not resolve
still fails after the retries, and terminal is the right answer for it.
"""

import socket

import pytest

from egress_broker import EgressBlocked, resolve_and_pin


class _FlakyResolver:
    """Fails the first `fail_times` lookups, then resolves normally."""

    def __init__(self, fail_times, ip="93.184.216.34"):
        self.fail_times = fail_times
        self.ip = ip
        self.calls = 0

    def getaddrinfo(self, host, port, *a, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (self.ip, port))]


def test_a_single_transient_dns_failure_is_retried(monkeypatch):
    """One dropped lookup must not fail the crawl."""
    resolver = _FlakyResolver(fail_times=1)
    monkeypatch.setattr(socket, "getaddrinfo", resolver.getaddrinfo)

    target = resolve_and_pin("https://beast-fit.com")

    assert target.ip == "93.184.216.34"
    assert resolver.calls == 2, "expected one retry after the transient failure"


def test_several_transient_failures_are_ridden_out(monkeypatch):
    """Concurrency-induced drops can bunch up; one retry is not enough."""
    resolver = _FlakyResolver(fail_times=2)
    monkeypatch.setattr(socket, "getaddrinfo", resolver.getaddrinfo)

    target = resolve_and_pin("https://be-modaco.com")

    assert target.ip == "93.184.216.34"


def test_a_host_that_never_resolves_is_still_blocked(monkeypatch):
    """Retrying must not turn a permanent failure into a hang or a pass."""
    resolver = _FlakyResolver(fail_times=99)
    monkeypatch.setattr(socket, "getaddrinfo", resolver.getaddrinfo)

    with pytest.raises(EgressBlocked):
        resolve_and_pin("https://no-such-host.invalid")

    assert resolver.calls <= 4, "retry budget must be bounded"


def test_a_forbidden_address_is_never_retried(monkeypatch):
    """A resolved-but-internal answer is a real block: fail fast, once.

    Retrying here would multiply the cost of every SSRF probe and could let a
    rebinding resolver hand back a public answer on a later attempt.
    """
    calls = {"n": 0}

    def internal(host, port, *a, **kw):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", port))]

    monkeypatch.setattr(socket, "getaddrinfo", internal)

    with pytest.raises(EgressBlocked):
        resolve_and_pin("https://metadata.example")

    assert calls["n"] == 1, "a forbidden IP must not be re-resolved"


def test_the_block_reason_stays_opaque(monkeypatch):
    """No DNS oracle: the caller cannot tell 'no such host' from 'forbidden'."""
    def gaierror(host, port, *a, **kw):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    def internal(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", gaierror)
    with pytest.raises(EgressBlocked) as unresolvable:
        resolve_and_pin("https://nope.invalid")

    monkeypatch.setattr(socket, "getaddrinfo", internal)
    with pytest.raises(EgressBlocked) as forbidden:
        resolve_and_pin("https://loopback.example")

    assert unresolvable.value.reason == forbidden.value.reason
