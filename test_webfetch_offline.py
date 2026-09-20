"""
test_webfetch_offline.py — real tests for tasks/webfetch.py's SSRF
hardening (_ensure_public_host, the no-redirect opener) and its
existing fetch/strip behavior. This sandbox has no general internet
access, so DNS resolution and the actual HTTP call are mocked via
unittest.mock -- what's under test is webfetch.py's own logic (scheme
checks, IP-range classification, wiring), not real network behavior.
"""

import socket
from unittest.mock import patch, MagicMock

from tasks.webfetch import fetch_url_text, _ensure_public_host, _NoRedirect, FetchError


def _addrinfo_for(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 443) if family == socket.AF_INET
             else (ip, 443, 0, 0))]


def test_rejects_non_http_scheme():
    for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/x"):
        try:
            _ensure_public_host(url)
            raise AssertionError(f"expected FetchError for {url!r}")
        except FetchError as e:
            assert "scheme" in str(e)
    print("PASS: non-http(s) schemes (file://, ftp://, gopher://) are rejected")


def test_rejects_url_with_no_hostname():
    try:
        _ensure_public_host("http:///no-host-here")
        raise AssertionError("expected FetchError for a URL with no hostname")
    except FetchError as e:
        assert "hostname" in str(e)
    print("PASS: a URL with no hostname is rejected")


def test_rejects_loopback_address():
    with patch("tasks.webfetch.socket.getaddrinfo", return_value=_addrinfo_for("127.0.0.1")):
        try:
            _ensure_public_host("http://localhost/x")
            raise AssertionError("expected FetchError for a loopback address")
        except FetchError as e:
            assert "127.0.0.1" in str(e)
    print("PASS: a hostname resolving to loopback (127.0.0.1) is rejected")


def test_rejects_private_range_address():
    with patch("tasks.webfetch.socket.getaddrinfo", return_value=_addrinfo_for("10.0.0.5")):
        try:
            _ensure_public_host("http://internal.example.com/x")
            raise AssertionError("expected FetchError for a private-range address")
        except FetchError as e:
            assert "10.0.0.5" in str(e)
    print("PASS: a hostname resolving to a private range (10.x) is rejected")


def test_rejects_link_local_cloud_metadata_address():
    """169.254.169.254 is the cloud metadata endpoint on AWS/GCP/Azure
    -- a hostname that resolves here is exactly the kind of SSRF target
    this check exists to block (credential theft via instance
    metadata)."""
    with patch("tasks.webfetch.socket.getaddrinfo", return_value=_addrinfo_for("169.254.169.254")):
        try:
            _ensure_public_host("http://metadata.example.com/x")
            raise AssertionError("expected FetchError for a link-local/metadata address")
        except FetchError as e:
            assert "169.254.169.254" in str(e)
    print("PASS: a hostname resolving to the cloud metadata range (169.254.x.x) is rejected")


def test_rejects_dns_resolution_failure():
    with patch("tasks.webfetch.socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
        try:
            _ensure_public_host("http://this-does-not-resolve.invalid/x")
            raise AssertionError("expected FetchError for a DNS resolution failure")
        except FetchError as e:
            assert "could not resolve" in str(e)
    print("PASS: a DNS resolution failure is a clean FetchError, not an unhandled exception")


def test_accepts_a_public_address():
    with patch("tasks.webfetch.socket.getaddrinfo", return_value=_addrinfo_for("93.184.216.34")):
        _ensure_public_host("https://example.com/x")  # must not raise
    print("PASS: a hostname resolving to a real public address is allowed through")


def test_redirect_handler_never_follows_a_redirect():
    handler = _NoRedirect()
    result = handler.redirect_request(
        req=MagicMock(), fp=MagicMock(), code=302, msg="Found",
        headers={}, newurl="http://internal.example.com/moved",
    )
    assert result is None, (
        "redirect_request() must return None so urllib never follows the redirect -- "
        "a validated URL could otherwise redirect to an internal address and bypass "
        "_ensure_public_host() entirely"
    )
    print("PASS: the redirect handler refuses to follow any redirect")


def test_fetch_url_text_end_to_end_success_path():
    """Proves the new validation gate is actually wired into
    fetch_url_text() and doesn't break the existing successful-fetch/
    strip/truncate behavior."""
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"<html><body><p>Hello world</p></body></html>"
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch("tasks.webfetch.socket.getaddrinfo", return_value=_addrinfo_for("93.184.216.34")), \
         patch("tasks.webfetch._opener.open", return_value=fake_resp) as mock_open:
        result = fetch_url_text("https://example.com/page")

    assert result == "Hello world"
    assert mock_open.call_count == 1
    print("PASS: fetch_url_text() validates the host, then fetches and strips normally")


def test_fetch_url_text_blocks_before_ever_opening_a_connection():
    """The validation must happen BEFORE the network call -- proves
    _opener.open() is never even reached for a blocked host."""
    with patch("tasks.webfetch.socket.getaddrinfo", return_value=_addrinfo_for("127.0.0.1")), \
         patch("tasks.webfetch._opener.open") as mock_open:
        try:
            fetch_url_text("http://localhost/admin")
            raise AssertionError("expected FetchError")
        except FetchError:
            pass
    assert mock_open.call_count == 0, "must never open a connection to a blocked host"
    print("PASS: a blocked host is rejected before any network call is made")


if __name__ == "__main__":
    test_rejects_non_http_scheme()
    test_rejects_url_with_no_hostname()
    test_rejects_loopback_address()
    test_rejects_private_range_address()
    test_rejects_link_local_cloud_metadata_address()
    test_rejects_dns_resolution_failure()
    test_accepts_a_public_address()
    test_redirect_handler_never_follows_a_redirect()
    test_fetch_url_text_end_to_end_success_path()
    test_fetch_url_text_blocks_before_ever_opening_a_connection()
    print("\nAll webfetch.py offline tests passed.")
