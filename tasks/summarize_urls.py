"""
tasks/summarize_urls.py — the first real (non-simulated) task type.

Narrow by design: fetch up to 3 URLs, strip them to plain text, ask the
model for a short factual summary of each. This is intentionally the
simplest possible real task — pure read-only research (permission_level
1-2), no side effects, nothing that could touch money or write anywhere
external. It exists to prove the wiring, not to be a finished research
agent.

Fetch logic lives in tasks/webfetch.py now (shared with
research_opportunity.py) — re-exported here so existing imports
(`from tasks.summarize_urls import fetch_url_text, FetchError`) and
existing tests keep working unchanged.
"""

from tasks.webfetch import fetch_url_text, FetchError, MAX_CHARS_PER_PAGE  # noqa: F401


def summarize_urls(urls, client) -> dict:
    """Returns {url: summary_or_error} for each URL. `client` is any
    object with a .complete(messages, system, max_tokens) method —
    pass a real AnthropicClient or a MockClient (see llm_client.py).
    This function makes ONE model call per URL that fetched successfully;
    URLs that fail to fetch are recorded as errors, never silently
    dropped or replaced with a guess."""
    if len(urls) > 3:
        raise ValueError("this narrow task type is capped at 3 URLs")

    results = {}
    for url in urls:
        try:
            page_text = fetch_url_text(url)
        except FetchError as e:
            results[url] = f"[FETCH FAILED: {e}]"
            continue

        if not page_text:
            results[url] = "[FETCHED PAGE HAD NO EXTRACTABLE TEXT]"
            continue

        summary = client.complete(
            system=(
                "You summarize a single web page in 2-3 sentences, in your "
                "own words, factually and neutrally. If the content is thin, "
                "ambiguous, or looks like an error/paywall page, say so "
                "plainly instead of inventing content."
            ),
            messages=[{"role": "user", "content": f"Page content:\n\n{page_text}"}],
            max_tokens=250,
        )
        results[url] = summary
    return results
