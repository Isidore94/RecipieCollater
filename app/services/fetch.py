"""SSRF-hardened HTTP fetch for ingestion.

A user can submit any URL, so the fetcher must refuse to reach the home network or cloud
metadata endpoints (docs/04-ingestion-pipeline.md; CONVENTIONS security). Defense in depth:

* every hostname is resolved and **all** its addresses are checked against private / loopback /
  link-local / reserved ranges before we connect - one internal address blocks the whole host;
* redirects are followed manually so each hop's target is re-validated (no redirect-to-internal);
* the response body is streamed with a hard size cap and a short timeout.

Residual gap (accepted for a self-hosted LAN app): a hostile DNS server could answer our
pre-flight lookup with a public address and httpx's connect with a private one (DNS rebinding).
Closing it means pinning the socket to the vetted IP; deferred until it matters here.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import httpx

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 5

# A realistic desktop UA; many recipe sites 403 an obvious bot. The paste/shortcut path is the
# fallback for sites that block anyway (returned as the ``fetch_blocked`` category).
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_BINARY_PREFIXES = ("image/", "video/", "audio/", "application/pdf", "application/octet-stream")


class FetchError(Exception):
    """A fetch failed; ``category`` maps to ingest_jobs.error_category for the inbox."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True, slots=True)
class FetchResult:
    final_url: str
    html: str
    content_type: str


def ip_is_public(ip: str) -> bool:
    """True only for a routable public address (IPv4-mapped IPv6 is unwrapped first)."""
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_host_is_public(host: str) -> None:
    """Resolve ``host`` and raise FetchError unless every resolved address is public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FetchError("fetch_dns", f"could not resolve host: {host}") from exc
    ips = {info[4][0] for info in infos}
    if not ips:
        raise FetchError("fetch_dns", f"host resolved to no addresses: {host}")
    blocked = sorted(ip for ip in ips if not ip_is_public(ip))
    if blocked:
        raise FetchError(
            "fetch_blocked", f"refusing to reach a private/internal address ({', '.join(blocked)})"
        )


def _read_capped(resp: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > MAX_BYTES:
            raise FetchError(
                "fetch_too_large", f"page exceeds the {MAX_BYTES // 1024 // 1024}MB cap"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch(url: str) -> FetchResult:
    """Fetch a public URL's HTML, validating SSRF safety at every redirect hop."""
    import httpx  # lazy (CONVENTIONS 4)

    current = url
    with httpx.Client(follow_redirects=False, timeout=TIMEOUT_SECONDS, headers=_HEADERS) as client:
        for _ in range(MAX_REDIRECTS + 1):
            host = urlsplit(current).hostname or ""
            if not host:
                raise FetchError("fetch_error", f"URL has no host: {current}")
            assert_host_is_public(host)
            try:
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("fetch_error", "redirect without a Location header")
                        current = str(httpx.URL(current).join(location))
                        continue
                    if resp.status_code == 403:
                        raise FetchError(
                            "fetch_blocked",
                            "the site blocked the request (403) - try the paste or Shortcut path",
                        )
                    if resp.status_code >= 400:
                        raise FetchError("fetch_http", f"the site returned HTTP {resp.status_code}")
                    content_type = resp.headers.get("content-type", "").lower()
                    if any(content_type.startswith(p) for p in _BINARY_PREFIXES):
                        raise FetchError("fetch_content", f"not an HTML page ({content_type})")
                    raw = _read_capped(resp)
                    encoding = resp.encoding or "utf-8"
            except httpx.HTTPError as exc:
                raise FetchError("fetch_error", f"request failed: {exc}") from exc
            return FetchResult(
                final_url=current,
                html=raw.decode(encoding, errors="replace"),
                content_type=content_type,
            )
    raise FetchError("fetch_error", "too many redirects")
