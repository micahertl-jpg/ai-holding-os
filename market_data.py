"""
market_data.py — minimal, real stock-quote client using only the Python
standard library (urllib), same pattern as llm_client.py.

Talks to Alpha Vantage's free GLOBAL_QUOTE endpoint. This is NOT a mock:
given a real ALPHAVANTAGE_API_KEY and normal internet access,
AlphaVantageClient.get_quote() makes an actual HTTPS GET and returns the
real latest traded price. It was written in a sandbox with no API key and
no general internet access, so the live call itself has NOT been executed
yet — see MockMarketDataClient below for what was actually exercised
there, and README.md for how to do the first real test run.

Why Alpha Vantage: free API key (instant signup, email only), simple
JSON, no extra pip dependency needed (plain urllib, same as the rest of
this codebase). Its free tier is rate-limited — check the current limit
on your own key at alphavantage.co, since it has changed over time and
this comment could go stale. A rate-limited/empty response comes back as
HTTP 200 with a "Note" or "Information" field instead of a real error
code, so that shape is checked explicitly below rather than trusting a
200 status alone — otherwise a rate-limit message could be silently
parsed as if it were quote data.
"""

import json
import os
import urllib.request
import urllib.error

ALPHAVANTAGE_API_URL = "https://www.alphavantage.co/query"
FETCH_TIMEOUT_SECONDS = 15


class MarketDataError(Exception):
    pass


class AlphaVantageClient:
    """Thin real client. One method: get_quote(). Swap this out for a
    different provider later by writing another class with the same
    `.get_quote(symbol) -> {"symbol", "price", "as_of", "mock"}`
    signature — nothing else in the codebase should need to change."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY")
        if not self.api_key:
            raise MarketDataError(
                "No ALPHAVANTAGE_API_KEY found (env var or constructor arg). "
                "Refusing to proceed — this system never fabricates market data."
            )

    def get_quote(self, symbol: str) -> dict:
        """Returns {"symbol": str, "price": float, "as_of": str, "mock": False}.
        Raises MarketDataError on any failure, including a rate-limited or
        malformed response — never returns a guessed/fabricated price."""
        url = (f"{ALPHAVANTAGE_API_URL}?function=GLOBAL_QUOTE&symbol="
               f"{urllib.request.quote(symbol)}&apikey={self.api_key}")
        req = urllib.request.Request(url, headers={"User-Agent": "ai-holding-os-trading/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise MarketDataError(f"failed to fetch quote for {symbol}: {e}") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MarketDataError(f"non-JSON response fetching quote for {symbol}: {e}") from e

        if "Note" in data or "Information" in data:
            raise MarketDataError(
                f"Alpha Vantage returned a rate-limit/info message instead of quote "
                f"data for {symbol}: {data.get('Note') or data.get('Information')}"
            )

        quote = data.get("Global Quote") or {}
        price_str = quote.get("05. price")
        if not price_str:
            raise MarketDataError(
                f"no '05. price' field in Alpha Vantage response for {symbol}: {data}"
            )
        try:
            price = float(price_str)
        except ValueError as e:
            raise MarketDataError(f"non-numeric price for {symbol}: {price_str!r}") from e
        if price <= 0:
            raise MarketDataError(f"non-positive price for {symbol}: {price}")

        return {
            "symbol": symbol,
            "price": price,
            "as_of": quote.get("07. latest trading day", ""),
            "mock": False,
        }


class MockMarketDataClient:
    """Explicit stand-in used ONLY when no real API key/network is
    available (e.g. this development sandbox, or an offline test that
    wants deterministic prices). Every quote is tagged mock=True so
    downstream code (tasks/trading_cycle.py) can refuse to paper-trade on
    fake prices instead of silently doing so — the same "never fabricate"
    rule llm_client.MockClient follows for model output, applied here to
    market data."""

    def __init__(self, prices: dict = None, default_price: float = 100.0):
        self.prices = prices or {}
        self.default_price = default_price

    def get_quote(self, symbol: str) -> dict:
        return {
            "symbol": symbol,
            "price": self.prices.get(symbol, self.default_price),
            "as_of": "MOCK",
            "mock": True,
        }


def get_default_client():
    """Returns a real AlphaVantageClient if a key is present, otherwise
    an explicitly-labeled MockMarketDataClient. Never silently pretends a
    mock price is real."""
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if key:
        return AlphaVantageClient(api_key=key)
    return MockMarketDataClient()
