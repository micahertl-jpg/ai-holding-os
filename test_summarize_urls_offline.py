"""
test_summarize_urls_offline.py — proves summarize_urls()'s logic works
when fetching succeeds, without depending on this sandbox's blocked
network. Uses unittest.mock to stand in for urllib and the LLM client.
This is a real correctness test of the code path, not a demo of a live
integration.
"""

import urllib.error
from unittest.mock import patch
from tasks.summarize_urls import summarize_urls
from llm_client import MockClient


class FakeResponse:
    def __init__(self, html):
        self._html = html.encode("utf-8")

    def read(self):
        return self._html

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _addrinfo_for(ip):
    import socket
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


def test_success_path():
    fake_html = "<html><body><h1>Hello</h1><p>This is a test page about widgets.</p></body></html>"
    with patch("tasks.webfetch.socket.getaddrinfo", return_value=_addrinfo_for("93.184.216.34")), \
         patch("tasks.webfetch._opener.open",
               return_value=FakeResponse(fake_html)):
        client = MockClient(canned_response="This page discusses widgets briefly.")
        results = summarize_urls(["https://example.com/fake"], client)

    assert results["https://example.com/fake"] == "This page discusses widgets briefly.", results
    print("PASS: success path — fetched (mocked) HTML -> stripped -> summarized correctly")


def test_over_limit_rejected():
    try:
        summarize_urls(["a", "b", "c", "d"], MockClient())
    except ValueError as e:
        print(f"PASS: correctly rejected >3 URLs ({e})")
        return
    raise AssertionError("expected ValueError for >3 URLs")


def test_fetch_failure_is_recorded_not_hidden():
    with patch("tasks.webfetch.socket.getaddrinfo", return_value=_addrinfo_for("93.184.216.34")), \
         patch("tasks.webfetch._opener.open",
               side_effect=urllib.error.URLError("connection refused")):
        results = summarize_urls(["https://example.com/down"], MockClient())
    assert "FETCH FAILED" in results["https://example.com/down"]
    print("PASS: fetch failure is recorded in the result, not swallowed or faked")


if __name__ == "__main__":
    test_success_path()
    test_over_limit_rejected()
    test_fetch_failure_is_recorded_not_hidden()
    print("\nAll offline correctness tests passed.")
