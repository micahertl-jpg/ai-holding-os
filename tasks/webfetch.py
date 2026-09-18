"""
tasks/webfetch.py — shared URL-fetching helper.

Extracted from summarize_urls.py (which was the first thing to need
it) so research_opportunity.py and any future task type can reuse the
exact same fetch behavior — including the gzip/encoding bug fix found
via real testing — instead of a second, silently-diverging copy.
"""

import re
import urllib.request
import urllib.error

MAX_CHARS_PER_PAGE = 6000
FETCH_TIMEOUT_SECONDS = 15


class FetchError(Exception):
    pass


def _strip_html(html: str) -> str:
    """Very crude tag stripper — good enough for these narrow task
    types. A production research agent should use a real HTML->text
    library instead; not adding one here to keep this dependency-free."""
    html = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_url_text(url: str, max_chars: int = MAX_CHARS_PER_PAGE) -> str:
    # Accept-Encoding: identity avoids gzip/br responses that urllib
    # won't auto-decompress, which otherwise produces garbled binary
    # text instead of a real fetch error — caught live via python.org
    # returning gzip content during real-world testing.
    req = urllib.request.Request(
        url, headers={"User-Agent": "ai-holding-os-research/0.1",
                       "Accept-Encoding": "identity"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
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
