"""
test_market_data_offline.py — real tests for market_data.py that don't
need network access: the response-shape parsing/validation logic (via a
fake urlopen), MockMarketDataClient's contract, and the daily request-
budget tracking (used_today()/reserve_budget()) against a real SQLite
Database with hand-seeded rows.
"""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import market_data
from db import Database, new_id
from scheduler import TIMESTAMP_FORMAT

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_market_data.db")

NOW = datetime(2026, 1, 2, 12, 0, 0)


def _fmt(dt):
    return dt.strftime(TIMESTAMP_FORMAT)


def _setup_db():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    return Database(TEST_DB_PATH)


class _FakeRealClient:
    """Stands in for AlphaVantageClient without needing a real API key
    -- reserve_budget() only ever checks .is_mock, nothing else."""
    is_mock = False


def _fake_response(payload_dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload_dict).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_get_quote_parses_a_real_shaped_response():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Global Quote": {"01. symbol": "AAPL", "05. price": "231.50",
                                 "07. latest trading day": "2026-01-02"}}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        quote = client.get_quote("AAPL")
    assert quote == {"symbol": "AAPL", "price": 231.50, "as_of": "2026-01-02", "mock": False}
    print("PASS: get_quote parses a real-shaped Alpha Vantage response correctly")


def test_get_quote_raises_on_rate_limit_note():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is ..."}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        try:
            client.get_quote("AAPL")
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "rate-limit" in str(e)
    print("PASS: a rate-limit 'Note' response raises MarketDataError instead of being "
          "silently parsed as if it were quote data")


def test_get_quote_raises_on_missing_price_field():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Global Quote": {}}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        try:
            client.get_quote("BADSYM")
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "no '05. price' field" in str(e)
    print("PASS: an empty Global Quote (e.g. invalid symbol) raises MarketDataError, "
          "never a fabricated price")


def test_client_refuses_to_construct_without_api_key():
    with patch.dict("os.environ", {}, clear=True):
        try:
            market_data.AlphaVantageClient(api_key=None)
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "ALPHAVANTAGE_API_KEY" in str(e)
    print("PASS: AlphaVantageClient refuses to construct without an API key -- never "
          "silently falls back to fabricated data")


def test_mock_client_is_clearly_labeled():
    client = market_data.MockMarketDataClient(prices={"AAPL": 123.45})
    quote = client.get_quote("AAPL")
    assert quote["price"] == 123.45
    assert quote["mock"] is True
    default_quote = market_data.MockMarketDataClient().get_quote("UNKNOWN")
    assert default_quote["mock"] is True
    print("PASS: MockMarketDataClient tags every quote mock=True so callers can refuse "
          "to trade on it")


def test_get_daily_history_parses_a_real_shaped_response_and_filters_by_date():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Time Series (Daily)": {
        "2026-01-05": {"1. open": "101.0", "2. high": "102.0", "3. low": "100.5",
                        "4. close": "101.5", "5. volume": "1000000"},
        "2026-01-02": {"1. open": "100.0", "2. high": "101.0", "3. low": "99.5",
                        "4. close": "100.8", "5. volume": "900000"},
        "2025-12-31": {"1. open": "99.0", "2. high": "99.5", "3. low": "98.5",
                       "4. close": "99.2", "5. volume": "800000"},
    }}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        bars = client.get_daily_history("AAPL", "2026-01-01", "2026-01-31")
    # 2025-12-31 is outside the requested range and must be excluded.
    assert [b["date"] for b in bars] == ["2026-01-02", "2026-01-05"], bars
    assert bars[0] == {"date": "2026-01-02", "open": 100.0, "high": 101.0, "low": 99.5,
                        "close": 100.8, "volume": 900000, "mock": False}
    print("PASS: get_daily_history parses a real-shaped response, filters to the requested "
          "date range, and returns bars oldest-first")


def test_get_daily_history_raises_on_rate_limit_note():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Note": "Thank you for using Alpha Vantage! ..."}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        try:
            client.get_daily_history("AAPL", "2026-01-01", "2026-01-31")
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "rate-limit" in str(e)
    print("PASS: get_daily_history raises on a rate-limit 'Note' response, never silently "
          "returns empty/fabricated history")


def test_get_daily_history_raises_when_range_has_no_bars():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Time Series (Daily)": {
        "2020-01-02": {"1. open": "1.0", "2. high": "1.0", "3. low": "1.0",
                        "4. close": "1.0", "5. volume": "1"},
    }}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        try:
            client.get_daily_history("AAPL", "2026-01-01", "2026-01-31")
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "no daily bars found" in str(e)
    print("PASS: get_daily_history raises loudly when the requested range has no bars, "
          "rather than silently backtesting against zero data")


def test_mock_client_daily_history_is_clearly_labeled_and_skips_weekends():
    client = market_data.MockMarketDataClient(prices={"AAPL": 100.0})
    bars = client.get_daily_history("AAPL", "2026-01-05", "2026-01-11")  # Mon..Sun
    assert all(b["mock"] is True for b in bars)
    weekdays = [market_data.datetime.date.fromisoformat(b["date"]).weekday() for b in bars]
    assert all(w < 5 for w in weekdays), "mock history must skip Sat/Sun like a real market"
    assert len(bars) == 5  # Mon-Fri
    print("PASS: MockMarketDataClient's daily history is tagged mock=True and skips weekends")


def test_get_default_client_picks_real_client_only_with_a_key():
    with patch.dict("os.environ", {"ALPHAVANTAGE_API_KEY": "real-key"}, clear=True):
        assert isinstance(market_data.get_default_client(), market_data.AlphaVantageClient)
    with patch.dict("os.environ", {}, clear=True):
        assert isinstance(market_data.get_default_client(), market_data.MockMarketDataClient)
    print("PASS: get_default_client() returns a real client only when a key is configured, "
          "an explicitly-labeled mock otherwise")


# --- used_today() / reserve_budget() ---

def test_used_today_sums_only_todays_reservations():
    db = _setup_db()
    yesterday = NOW - timedelta(days=1)
    db.execute("INSERT INTO market_data_usage (id, purpose, request_count, created_at) "
               "VALUES (?,?,?,?)", (new_id("mdusage"), "trading_cycle", 5, _fmt(yesterday)))
    db.execute("INSERT INTO market_data_usage (id, purpose, request_count, created_at) "
               "VALUES (?,?,?,?)", (new_id("mdusage"), "trading_cycle", 3, _fmt(NOW)))
    db.execute("INSERT INTO market_data_usage (id, purpose, request_count, created_at) "
               "VALUES (?,?,?,?)", (new_id("mdusage"), "strategy_backtest_search", 4, _fmt(NOW)))

    assert market_data.used_today(db, now=NOW) == 7
    print("PASS: used_today sums only reservations from today (UTC), across every purpose")
    db.close()
    os.remove(TEST_DB_PATH)


def test_used_today_is_zero_with_no_reservations_yet():
    db = _setup_db()
    assert market_data.used_today(db, now=NOW) == 0
    print("PASS: used_today is a real, honest zero with nothing reserved yet")
    db.close()
    os.remove(TEST_DB_PATH)


def test_reserve_budget_is_a_no_op_for_a_mock_client():
    db = _setup_db()
    mock_client = market_data.MockMarketDataClient()
    # Deliberately over any real limit -- must never raise or record
    # anything, since no real quota is at stake with a mock client.
    market_data.reserve_budget(db, mock_client, 999, "trading_cycle", now=NOW)
    assert market_data.used_today(db, now=NOW) == 0
    print("PASS: reserve_budget is a complete no-op for a mock client -- no check, no record")
    db.close()
    os.remove(TEST_DB_PATH)


def test_reserve_budget_records_usage_for_a_real_client_within_budget():
    db = _setup_db()
    with patch.object(market_data, "DAILY_REQUEST_LIMIT", 25):
        market_data.reserve_budget(db, _FakeRealClient(), 5, "trading_cycle", now=NOW)
    assert market_data.used_today(db, now=NOW) == 5
    row = db.query_one("SELECT * FROM market_data_usage")
    assert row["purpose"] == "trading_cycle"
    assert row["request_count"] == 5
    print("PASS: reserve_budget records a real reservation for a real client within budget")
    db.close()
    os.remove(TEST_DB_PATH)


def test_reserve_budget_raises_and_records_nothing_once_the_daily_quota_is_exhausted():
    db = _setup_db()
    with patch.object(market_data, "DAILY_REQUEST_LIMIT", 10):
        market_data.reserve_budget(db, _FakeRealClient(), 8, "strategy_backtest_search", now=NOW)
        try:
            market_data.reserve_budget(db, _FakeRealClient(), 5, "trading_cycle", now=NOW)
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "5 Alpha Vantage request(s)" in str(e)
            assert "only 2 remain" in str(e)
            assert "8 already used" in str(e)

    # The refused reservation must never partially apply.
    assert market_data.used_today(db, now=NOW) == 8
    print("PASS: reserve_budget refuses loudly (before making any real calls) once a "
          "reservation would exceed what's left today, and never partially records it")
    db.close()
    os.remove(TEST_DB_PATH)


def test_reserve_budget_respects_a_raised_limit_for_an_upgraded_plan():
    db = _setup_db()
    with patch.object(market_data, "DAILY_REQUEST_LIMIT", 500):
        # Would have failed against the free-tier default of 25.
        market_data.reserve_budget(db, _FakeRealClient(), 100, "strategy_backtest_search", now=NOW)
    assert market_data.used_today(db, now=NOW) == 100
    print("PASS: reserve_budget respects ALPHAVANTAGE_DAILY_REQUEST_LIMIT for an upgraded plan")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_get_quote_parses_a_real_shaped_response()
    test_get_quote_raises_on_rate_limit_note()
    test_get_quote_raises_on_missing_price_field()
    test_client_refuses_to_construct_without_api_key()
    test_mock_client_is_clearly_labeled()
    test_get_daily_history_parses_a_real_shaped_response_and_filters_by_date()
    test_get_daily_history_raises_on_rate_limit_note()
    test_get_daily_history_raises_when_range_has_no_bars()
    test_mock_client_daily_history_is_clearly_labeled_and_skips_weekends()
    test_get_default_client_picks_real_client_only_with_a_key()
    test_used_today_sums_only_todays_reservations()
    test_used_today_is_zero_with_no_reservations_yet()
    test_reserve_budget_is_a_no_op_for_a_mock_client()
    test_reserve_budget_records_usage_for_a_real_client_within_budget()
    test_reserve_budget_raises_and_records_nothing_once_the_daily_quota_is_exhausted()
    test_reserve_budget_respects_a_raised_limit_for_an_upgraded_plan()
    print("\nAll market_data.py offline tests passed.")
