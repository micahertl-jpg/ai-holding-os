"""
llm_client.py — a minimal, real Anthropic Messages API client using only
the Python standard library (urllib), so it has zero pip dependencies.

This is NOT a mock. Given a real ANTHROPIC_API_KEY and normal internet
access, `AnthropicClient.complete()` makes an actual HTTPS POST to
https://api.anthropic.com/v1/messages and returns the model's real text
response. It was written and syntax/logic-checked in a sandbox with no
API key and no general internet access, so the live call itself has
NOT been executed yet — see MockClient below for what was actually
exercised in that sandbox, and README.md for how to do the first real
test run.
"""

import json
import os
import urllib.request
import urllib.error

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"

# Estimated USD cost per token, by model, used ONLY to give the Banker's
# ARC accounting (banker.py, executor.py) a real number to work with
# instead of a hardcoded 0.0. These are approximate list-price figures
# and are NOT read from Anthropic's account/billing API — they will
# drift out of date and don't reflect any volume/plan-specific pricing
# you actually have. Treat cost_usd on every response as an estimate
# for internal relative comparison (which tasks/agents are expensive),
# never as an exact reconciliation against a real invoice. Update this
# table if you change models or if pricing changes.
PRICING_USD_PER_TOKEN = {
    "claude-sonnet-4-6": {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},
}
DEFAULT_PRICING = {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000}


class LLMCallError(Exception):
    pass


class AnthropicClient:
    """Thin real client. One method: complete(). Swap this out for a
    different provider later by writing another class with the same
    `.complete(system, messages, max_tokens) -> str` signature — nothing
    else in the codebase should need to change (model-abstraction layer,
    per the project spec's Sec. 25)."""

    def __init__(self, api_key: str = None, model: str = DEFAULT_MODEL):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMCallError(
                "No ANTHROPIC_API_KEY found (env var or constructor arg). "
                "Refusing to proceed — this system never fabricates model output."
            )
        self.model = model
        # Set after every complete() call — see PRICING_USD_PER_TOKEN above
        # for what cost_usd actually means (an estimate, not a real invoice
        # lookup). Callers (executor.py) read this right after complete()
        # returns to feed the Banker's ARC cost accounting.
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def complete(self, messages, system=None, max_tokens=1000) -> str:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system

        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise LLMCallError(f"Anthropic API HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMCallError(f"Network error calling Anthropic API: {e}") from e

        text_parts = [block["text"] for block in data.get("content", [])
                      if block.get("type") == "text"]
        if not text_parts:
            raise LLMCallError(f"No text content in Anthropic response: {data}")

        usage = data.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        pricing = PRICING_USD_PER_TOKEN.get(self.model, DEFAULT_PRICING)
        cost_usd = (input_tokens * pricing["input"]) + (output_tokens * pricing["output"])
        self.last_usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }

        return "\n".join(text_parts)


class MockClient:
    """Explicit stand-in used ONLY when no real API key/network is
    available (e.g. this development sandbox). Every response is
    prefixed so it can never be mistaken for a real model output further
    down the pipeline or in a log/dashboard."""

    def __init__(self, canned_response: str = None):
        self.canned_response = canned_response
        # Always zero — no real API call happens, so there is no real
        # cost. Kept as the same shape as AnthropicClient.last_usage so
        # executor.py can read it uniformly regardless of which client
        # is in use.
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def complete(self, messages, system=None, max_tokens=1000) -> str:
        last_user = next((m["content"] for m in reversed(messages)
                           if m["role"] == "user"), "")
        preview = (last_user[:120] + "...") if len(last_user) > 120 else last_user
        return self.canned_response or (
            f"[MOCK RESPONSE — no live Anthropic API call made. "
            f"Prompt was: {preview!r}]"
        )


class CostTrackingClient:
    """Wraps any real client (AnthropicClient or MockClient) and
    accumulates cost across MULTIPLE .complete() calls made while
    executing a single task — e.g. summarize_urls makes one call per
    URL. Reading the wrapped client's own `.last_usage` after the task
    finishes would only reflect the LAST of those calls, silently
    undercounting cost for any handler that calls complete() more than
    once. executor.py creates one of these per task (not per run_once()
    pass, which can process several tasks) so `total_cost_usd` is
    exactly that task's real cost, never mixed with another task's.

    Duck-types the same `.complete(messages, system, max_tokens) -> str`
    signature as the wrapped client, so it's a drop-in replacement
    everywhere a client is expected — no changes needed to
    summarize_urls.py / research_opportunity.py / research_roblox_trend.py."""

    def __init__(self, inner_client):
        self.inner = inner_client
        self.total_cost_usd = 0.0
        self.call_count = 0

    def complete(self, *args, **kwargs) -> str:
        result = self.inner.complete(*args, **kwargs)
        usage = getattr(self.inner, "last_usage", None) or {}
        self.total_cost_usd += usage.get("cost_usd", 0.0)
        self.call_count += 1
        return result


def get_default_client():
    """Returns a real AnthropicClient if a key is present, otherwise an
    explicitly-labeled MockClient. Never silently pretends a mock result
    is real."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return AnthropicClient(api_key=key)
    return MockClient()
