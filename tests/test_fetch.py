"""SSRF guard: only routable public addresses are reachable, and a host that resolves to any
private/internal address is refused (docs/04 fetch safety). No network is touched - getaddrinfo
is monkeypatched."""

from __future__ import annotations

import socket

import pytest

from app.services import fetch


@pytest.mark.parametrize(
    "ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]
)
def test_public_ips_allowed(ip: str) -> None:
    assert fetch.ip_is_public(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.10",  # private
        "172.16.5.4",  # private
        "169.254.169.254",  # link-local / cloud metadata
        "0.0.0.0",  # unspecified
        "::1",  # ipv6 loopback
        "fe80::1",  # ipv6 link-local
        "::ffff:127.0.0.1",  # ipv4-mapped loopback
        "::ffff:10.0.0.1",  # ipv4-mapped private
        "not-an-ip",
    ],
)
def test_non_public_ips_blocked(ip: str) -> None:
    assert fetch.ip_is_public(ip) is False


def _resolves_to(*ips: str) -> object:
    infos = [(0, 0, 0, "", (ip, 0)) for ip in ips]
    return lambda *a, **k: infos


def test_public_host_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo",_resolves_to("93.184.216.34"))
    fetch.assert_host_is_public("example.com")  # does not raise


def test_private_host_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo",_resolves_to("127.0.0.1"))
    with pytest.raises(fetch.FetchError) as exc:
        fetch.assert_host_is_public("localhost.evil.test")
    assert exc.value.category == "fetch_blocked"


def test_host_with_any_private_address_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host resolving to both a public and a private address is refused (rebind defense).
    monkeypatch.setattr(socket, "getaddrinfo",_resolves_to("93.184.216.34", "10.0.0.1"))
    with pytest.raises(fetch.FetchError):
        fetch.assert_host_is_public("rebind.test")


def test_dns_failure_is_fetch_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> list[object]:
        raise OSError("nxdomain")

    monkeypatch.setattr(socket, "getaddrinfo",boom)
    with pytest.raises(fetch.FetchError) as exc:
        fetch.assert_host_is_public("nope.invalid")
    assert exc.value.category == "fetch_dns"
