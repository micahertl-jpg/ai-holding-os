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

import datetime
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

    def get_daily_history(self, symbol: str, start_date: str, end_date: str) -> list:
        """Returns real historical daily bars for symbol, inclusive of
        start_date/end_date (both "YYYY-MM-DD"), oldest first: a list of
        {"date", "open", "high", "low", "close", "volume", "mock": False}.
        Used by tasks/backtest.py to test a strategy against real past
        price action rather than live quotes. Raises MarketDataError on
        any failure -- never fabricates a historical bar.

        Uses outputsize=compact (the last ~100 trading days, roughly
        4-5 calendar months) rather than "full". outputsize=full is now
        a premium-only parameter on Alpha Vantage's free tier -- found
        live, the hard way: an earlier version of this code requested
        "full" on the assumption free accounts still got 20+ years of
        history, and every real call failed with "The outputsize=full
        parameter value is a premium feature." A requested date range
        older than what "compact" covers simply returns fewer bars for
        the earlier portion (or, if the WHOLE range predates it,
        MarketDataError via the "no daily bars found" check below) --
        never a fabricated bar to fill the gap."""
        url = (f"{ALPHAVANTAGE_API_URL}?function=TIME_SERIES_DAILY&symbol="
               f"{urllib.request.quote(symbol)}&outputsize=compact&apikey={self.api_key}")
        req = urllib.request.Request(url, headers={"User-Agent": "ai-holding-os-trading/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise MarketDataError(f"failed to fetch daily history for {symbol}: {e}") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MarketDataError(f"non-JSON response fetching daily history for {symbol}: {e}") from e

        if "Note" in data or "Information" in data:
            raise MarketDataError(
                f"Alpha Vantage returned a rate-limit/info message instead of history "
                f"data for {symbol}: {data.get('Note') or data.get('Information')}"
            )

        series = data.get("Time Series (Daily)")
        if not series:
            raise MarketDataError(
                f"no 'Time Series (Daily)' field in Alpha Vantage response for {symbol}: {data}"
            )

        bars = []
        for date_str, values in series.items():
            if date_str < start_date or date_str > end_date:
                continue
            try:
                bars.append({
                    "date": date_str,
                    "open": float(values["1. open"]),
                    "high": float(values["2. high"]),
                    "low": float(values["3. low"]),
                    "close": float(values["4. close"]),
                    "volume": int(float(values["5. volume"])),
                    "mock": False,
                })
            except (KeyError, ValueError) as e:
                raise MarketDataError(f"malformed daily bar for {symbol} on {date_str}: {values}") from e

        bars.sort(key=lambda b: b["date"])
        if not bars:
            raise MarketDataError(
                f"no daily bars found for {symbol} in range {start_date}..{end_date} "
                f"(check the date range and that the symbol/dates are valid)"
            )
        return bars


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

    def get_daily_history(self, symbol: str, start_date: str, end_date: str) -> list:
        """Deterministic synthetic daily bars (a simple oscillation
        around self.prices[symbol]/default_price) for offline tests --
        never meant to resemble real market behavior. Every bar is
        tagged mock=True so tasks/backtest.py refuses to backtest
        against it outside of an explicit test, same rule get_quote()
        already follows for live paper/live trading."""
        base_price = self.prices.get(symbol, self.default_price)
        start = datetime.date.fromisoformat(start_date)
        end = datetime.date.fromisoformat(end_date)
        bars = []
        d = start
        day_index = 0
        while d <= end:
            if d.weekday() < 5:  # markets aren't open on weekends
                price = base_price * (1 + 0.01 * ((day_index % 10) - 5))
                bars.append({
                    "date": d.isoformat(), "open": price, "high": price * 1.01,
                    "low": price * 0.99, "close": price, "volume": 1000000,
                    "mock": True,
                })
                day_index += 1
            d += datetime.timedelta(days=1)
        return bars


def get_default_client():
    """Returns a real AlphaVantageClient if a key is present, otherwise
    an explicitly-labeled MockMarketDataClient. Never silently pretends a
    mock price is real."""
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if key:
        return AlphaVantageClient(api_key=key)
    return MockMarketDataClient()
