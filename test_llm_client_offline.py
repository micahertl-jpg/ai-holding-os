"""
test_llm_client_offline.py — real tests for llm_client.py's cost/usage
tracking, added when executor.py's ARC accounting was wired to use real
(estimated) LLM cost instead of hardcoded 0.0. Uses unittest.mock to
stand in for the actual HTTPS call (this sandbox has no internet/API
key), with a fake response shaped exactly like a real Anthropic
Messages API response including a `usage` block.
"""

import json
from unittest.mock import patch, MagicMock

from llm_client import AnthropicClient, MockClient, CostTrackingClient, PRICING_USD_PER_TOKEN


def _fake_response(text="A response.", input_tokens=100, output_tokens=50):
    body = json.dumps({
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body
    return cm


def test_anthropic_client_computes_real_cost_from_usage():
    client = AnthropicClient(api_key="fake-key-for-test")
    with patch("urllib.request.urlopen", return_value=_fake_response(
            input_tokens=1000, output_tokens=500)):
        result = client.complete(messages=[{"role": "user", "content": "hi"}])

    assert result == "A response."
    pricing = PRICING_USD_PER_TOKEN[client.model]
    expected_cost = 1000 * pricing["input"] + 500 * pricing["output"]
    assert client.last_usage["input_tokens"] == 1000
    assert client.last_usage["output_tokens"] == 500
    assert abs(client.last_usage["cost_usd"] - expected_cost) < 1e-12
    assert client.last_usage["cost_usd"] > 0, "a real call with real tokens must have nonzero cost"
    print("PASS: AnthropicClient.last_usage reflects the real token counts and a real cost estimate")


def test_anthropic_client_missing_usage_defaults_to_zero_not_a_crash():
    client = AnthropicClient(api_key="fake-key-for-test")
    body = json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body
    with patch("urllib.request.urlopen", return_value=cm):
        client.complete(messages=[{"role": "user", "content": "hi"}])
    assert client.last_usage == {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    print("PASS: a response with no usage block degrades to zero cost, not an exception")


def test_mock_client_always_reports_zero_cost():
    client = MockClient(canned_response="whatever")
    client.complete(messages=[{"role": "user", "content": "hi"}])
    assert client.last_usage == {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}, (
        "MockClient makes no real API call, so it must never report a nonzero cost"
    )
    print("PASS: MockClient.last_usage is always zero — never fabricates a cost for a fake call")


def test_cost_tracking_client_accumulates_across_multiple_calls():
    """The exact scenario CostTrackingClient exists for: summarize_urls
    calls .complete() once per URL. Reading the inner client's own
    last_usage after all calls finish would only see the LAST call,
    silently undercounting. The wrapper must sum every call."""
    inner = AnthropicClient(api_key="fake-key-for-test")
    tracked = CostTrackingClient(inner)

    with patch("urllib.request.urlopen", return_value=_fake_response(
            input_tokens=100, output_tokens=50)):
        tracked.complete(messages=[{"role": "user", "content": "first"}])
    with patch("urllib.request.urlopen", return_value=_fake_response(
            input_tokens=200, output_tokens=100)):
        tracked.complete(messages=[{"role": "user", "content": "second"}])

    pricing = PRICING_USD_PER_TOKEN[inner.model]
    expected_total = (
        (100 * pricing["input"] + 50 * pricing["output"]) +
        (200 * pricing["input"] + 100 * pricing["output"])
    )
    assert tracked.call_count == 2
    assert abs(tracked.total_cost_usd - expected_total) < 1e-12, (
        f"expected {expected_total}, got {tracked.total_cost_usd} — "
        f"a wrapper that only kept the last call's cost would fail this"
    )
    print("PASS: CostTrackingClient correctly sums cost across multiple .complete() calls, "
          "not just the most recent one")


def test_cost_tracking_client_with_mock_client_is_always_zero():
    tracked = CostTrackingClient(MockClient(canned_response="x"))
    tracked.complete(messages=[{"role": "user", "content": "a"}])
    tracked.complete(messages=[{"role": "user", "content": "b"}])
    assert tracked.total_cost_usd == 0.0
    assert tracked.call_count == 2
    print("PASS: wrapping a MockClient never produces a nonzero total cost, however many calls")


if __name__ == "__main__":
    test_anthropic_client_computes_real_cost_from_usage()
    test_anthropic_client_missing_usage_defaults_to_zero_not_a_crash()
    test_mock_client_always_reports_zero_cost()
    test_cost_tracking_client_accumulates_across_multiple_calls()
    test_cost_tracking_client_with_mock_client_is_always_zero()
    print("\nAll llm_client.py offline tests passed.")
