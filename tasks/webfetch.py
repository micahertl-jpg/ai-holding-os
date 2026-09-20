"""
tasks/webfetch.py — shared URL-fetching helper.

Extracted from summarize_urls.py (which was the first thing to need
it) so research_opportunity.py and any future task type can reuse the
exact same fetch behavior — including the gzip/encoding bug fix found
via real testing — instead of a second, silently-diverging copy.

SSRF hardening: `reference_urls` on every research task type ultimately
reaches this function, and while today that's only reachable through
the dashboard's own (authenticated) research-request endpoints -- the
public storefront checkout never accepts reference_urls -- a URL
fetcher with no scheme/host validation is a real SSRF primitive
regardless of who can currently reach it: server-side, resolves DNS,
and had no way to refuse an internal address before this. Only
http/https is allowed, the resolved address (not just the literal
hostname string, so a hostname that merely points at an internal IP is
still caught) must be public/routable, and redirects are never
followed (a validated URL could otherwise redirect to an internal
address and bypass the check entirely).
"""

import ipaddress
import re
import socket
import urllib.request
import urllib.error
from urllib.parse import urlparse

MAX_CHARS_PER_PAGE = 6000
FETCH_TIMEOUT_SECONDS = 15
ALLOWED_SCHEMES = {"http", "https"}


class FetchError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Returning None here tells urllib not to follow the redirect --
    the 3xx response is returned as-is instead. A followed redirect
    would let a URL that passes _ensure_public_host() at request time
    silently resolve somewhere else entirely by response time,
    defeating the whole point of validating the host first."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _ensure_public_host(url: str) -> None:
    """Raises FetchError unless `url` is http(s) with a hostname that
    resolves to a public, routable address. Blocks SSRF against
    internal infrastructure (loopback, link-local -- which is also
    where cloud metadata endpoints like 169.254.169.254 live --
    private ranges, multicast, reserved). Resolving DNS here (rather
    than only pattern-matching the literal hostname string) is
    required: a hostname an attacker controls can point at any IP they
    like regardless of what the hostname itself looks like."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchError(f"unsupported URL scheme {parsed.scheme!r} in {url!r} — "
                          f"only http/https are allowed")
    if not parsed.hostname:
        raise FetchError(f"URL has no hostname: {url!r}")
    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise FetchError(f"could not resolve host {parsed.hostname!r}: {e}") from e
    for family, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise FetchError(
                f"refusing to fetch {url!r} — {parsed.hostname!r} resolves to "
                f"{ip}, a non-public address"
            )


def _strip_html(html: str) -> str:
    """Very crude tag stripper — good enough for these narrow task
    types. A production research agent should use a real HTML->text
    library instead; not adding one here to keep this dependency-free."""
    html = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_url_text(url: str, max_chars: int = MAX_CHARS_PER_PAGE) -> str:
    _ensure_public_host(url)
    # Accept-Encoding: identity avoids gzip/br responses that urllib
    # won't auto-decompress, which otherwise produces garbled binary
    # text instead of a real fetch error — caught live via python.org
    # returning gzip content during real-world testing.
    req = urllib.request.Request(
        url, headers={"User-Agent": "ai-holding-os-research/0.1",
                       "Accept-Encoding": "identity"},
    )
    try:
        with _opener.open(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            raw_bytes = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        raise FetchError(f"failed to fetch {url}: {e}") from e

    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Still binary/undecodable even without compression (e.g. some
        # other encoding, or genuinely non-text content) — treat this as
        # a fetch failure rather than feeding garbage to the model.
        raise FetchError(f"{url} returned non-UTF-8/binary content, not fetchable as text")

    return _strip_html(raw)[:max_chars]
