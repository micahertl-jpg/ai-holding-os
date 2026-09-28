"""
market_data.py — minimal, real stock-quote client using only the Python
standard library (urllib), same pattern as llm_client.py.

Talks to Twelve Data's free /quote and /time_series endpoints. This is
NOT a mock: given a real TWELVEDATA_API_KEY and normal internet access,
TwelveDataClient.get_quote() makes an actual HTTPS GET and returns the
real latest traded price. It was written in a sandbox with no API key and
no general internet access, so the live call itself has NOT been executed
yet — see MockMarketDataClient below for what was actually exercised
there, and README.md for how to do the first real test run.

Why Twelve Data (replacing an earlier Alpha Vantage integration, found
live to be unworkable — Alpha Vantage's free tier caps out at 25
requests/day, which even a single default 5-symbol watchlist on a 6-hour
cycle burns through almost entirely on its own, one bad rate-limit-hit
day away from failing every subsequent cycle): free API key (instant
signup, email only), simple JSON, no extra pip dependency needed (plain
urllib, same as the rest of this codebase), and a free tier that covers
both live quotes AND historical daily bars (needed for backtesting) with
a much larger daily allowance. Its free tier is still rate-limited —
check the current limit on your own key at twelvedata.com, since it can
change over time and this comment could go stale. An error (bad symbol,
rate limit, invalid key) comes back as JSON with a "status": "error"
field — sometimes alongside a real non-200 HTTP status, sometimes not —
so that shape is checked explicitly below rather than trusting a 200
status alone, same discipline the codebase already applied to Alpha
Vantage's equivalent "Note"/"Information" quirk.
"""

import datetime
import json
import os
import urllib.request
import urllib.error

from db import new_id
from scheduler import TIMESTAMP_FORMAT

TWELVEDATA_API_URL = "https://api.twelvedata.com"
FETCH_TIMEOUT_SECONDS = 15

# Twelve Data's free-tier cap (800 requests/day at the time this was
# written -- see the module docstring's note that this can change).
# Every real caller (tasks/trading_cycle.py's paper AND live cycles,
# executor.py's strategy_backtest_search handler) shares this same
# quota, since they all hit the same real Twelve Data account. If
# you've upgraded to a paid Twelve Data plan, raise this via the env
# var rather than editing the default.
DAILY_REQUEST_LIMIT = int(os.environ.get("TWELVEDATA_DAILY_REQUEST_LIMIT", "800"))


class MarketDataError(Exception):
    pass


class TwelveDataClient:
    """Thin real client. Same two-method interface the codebase already
    depends on: `.get_quote(symbol) -> {"symbol", "price", "as_of", "mock"}`
    and `.get_daily_history(symbol, start_date, end_date) -> [bars]` —
    swap this out for yet another provider later by writing another class
    with the same signature, nothing else in the codebase should need to
    change."""

    is_mock = False

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("TWELVEDATA_API_KEY")
        if not self.api_key:
            raise MarketDataError(
                "No TWELVEDATA_API_KEY found (env var or constructor arg). "
                "Refusing to proceed — this system never fabricates market data."
            )

    def _get_json(self, path_and_query: str, symbol: str, action: str) -> dict:
        """Shared GET + error-surfacing for both endpoints below. Twelve
        Data returns a real non-200 status for some errors (auth, rate
        limit) and a 200 with a JSON {"status": "error"} body for others
        (bad symbol) -- both are handled here so a rate-limit message can
        never be mistaken for real data."""
        url = f"{TWELVEDATA_API_URL}/{path_and_query}&apikey={self.api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "ai-holding-os-trading/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("message", "")
            except Exception:
                detail = ""
            raise MarketDataError(
                f"failed to {action} for {symbol}: HTTP {e.code} {detail or e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise MarketDataError(f"failed to {action} for {symbol}: {e}") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MarketDataError(f"non-JSON response trying to {action} for {symbol}: {e}") from e

        if data.get("status") == "error" or ("code" in data and "message" in data):
            raise MarketDataError(
                f"Twelve Data returned an error instead of data trying to {action} "
                f"for {symbol}: {data.get('message', data)}"
            )
        return data

    def get_quote(self, symbol: str) -> dict:
        """Returns {"symbol": str, "price": float, "as_of": str, "mock": False}.
        Raises MarketDataError on any failure, including a rate-limited or
        malformed response — never returns a guessed/fabricated price."""
        data = self._get_json(f"quote?symbol={urllib.request.quote(symbol)}", symbol, "fetch quote")

        price_str = data.get("close")
        if not price_str:
            raise MarketDataError(
                f"no 'close' field in Twelve Data response for {symbol}: {data}"
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
            "as_of": data.get("datetime", ""),
            "mock": False,
        }

    def get_daily_history(self, symbol: str, start_date: str, end_date: str) -> list:
        """Returns real historical daily bars for symbol, inclusive of
        start_date/end_date (both "YYYY-MM-DD"), oldest first: a list of
        {"date", "open", "high", "low", "close", "volume", "mock": False}.
        Used by tasks/backtest.py to test a strategy against real past
        price action rather than live quotes. Raises MarketDataError on
        any failure -- never fabricates a historical bar.

        Passes start_date/end_date straight through to Twelve Data's own
        range filtering, then filters again locally in case the account's
        history depth doesn't reach as far back as requested -- a
        requested range older than what's available simply returns fewer
        bars for the earlier portion (or, if the WHOLE range predates it,
        MarketDataError via the "no daily bars found" check below), never
        a fabricated bar to fill the gap."""
        data = self._get_json(
            f"time_series?symbol={urllib.request.quote(symbol)}&interval=1day"
            f"&start_date={start_date}&end_date={end_date}&outputsize=5000",
            symbol, "fetch daily history",
        )

        values = data.get("values")
        if not values:
            raise MarketDataError(
                f"no 'values' field in Twelve Data response for {symbol}: {data}"
            )

        bars = []
        for row in values:
            date_str = row.get("datetime", "")[:10]
            if date_str < start_date or date_str > end_date:
                continue
            try:
                bars.append({
                    "date": date_str,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(float(row.get("volume") or 0)),
                    "mock": False,
                })
            except (KeyError, ValueError) as e:
                raise MarketDataError(f"malformed daily bar for {symbol} on {date_str}: {row}") from e

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

    is_mock = True

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


def used_today(db, now: datetime.datetime = None) -> int:
    """Real count of Twelve Data requests reserved so far today (UTC),
    summed from market_data_usage -- never estimated. Pure enough to
    unit-test directly against a real (SQLite) Database with hand-seeded
    rows."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row = db.query_one(
        "SELECT COALESCE(SUM(request_count),0) as total FROM market_data_usage WHERE created_at >= ?",
        (start_of_day.strftime(TIMESTAMP_FORMAT),),
    )
    return row["total"]


def reserve_budget(db, market_client, count: int, purpose: str, now: datetime.datetime = None) -> None:
    """Call BEFORE a caller is about to make `count` real Twelve Data
    requests (one per symbol it's about to fetch a quote/history for),
    so a request batch that would blow through the rest of today's
    quota is refused loudly up front -- never partway through, after
    already burning what was left. A no-op (no check, no record) when
    market_client.is_mock is True: there's no real quota at stake using
    MockMarketDataClient, and requiring db there too would force every
    existing offline test that passes a mock client to also thread a
    real Database through call sites that don't otherwise need one.

    Found necessary for real: a backtest search retried several times
    (each attempt burns real quota even when the provider rejects it
    with a rate-limit response, not a network failure) exhausted the
    day's quota right before a scheduled trading cycle needed it --
    trading_cycle and strategy_backtest_search were competing for the
    same shared resource with neither aware of the other's usage."""
    if getattr(market_client, "is_mock", False):
        return
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    used = used_today(db, now)
    remaining = max(0, DAILY_REQUEST_LIMIT - used)
    if count > remaining:
        raise MarketDataError(
            f"this would need {count} Twelve Data request(s), but only {remaining} remain "
            f"of today's {DAILY_REQUEST_LIMIT}/day free-tier quota ({used} already used) -- "
            f"refusing to start {purpose} and burn through what's left on a request that "
            f"would fail partway anyway. Try again after the quota resets, or raise "
            f"TWELVEDATA_DAILY_REQUEST_LIMIT if you've upgraded your Twelve Data plan."
        )
    db.execute(
        "INSERT INTO market_data_usage (id, purpose, request_count) VALUES (?, ?, ?)",
        (new_id("mdusage"), purpose, count),
    )


def get_default_client():
    """Returns a real TwelveDataClient if a key is present, otherwise
    an explicitly-labeled MockMarketDataClient. Never silently pretends a
    mock price is real."""
    key = os.environ.get("TWELVEDATA_API_KEY")
    if key:
        return TwelveDataClient(api_key=key)
    return MockMarketDataClient()
