"""
test_trading_strategy_override_offline.py — calls api.py's real
override_trading_strategy() directly (same technique
test_manual_triggers_offline.py uses), focused on the automatic
(ATR-based) stop-loss/take-profit path: when the owner's override
includes an auto_stop_loss_atr_multiple/auto_take_profit_atr_multiple,
the saved version's stop_loss_pct/take_profit_pct must reflect REAL
historical volatility immediately -- not wait for the next scheduled
trading_strategy_review to take effect.

Requires fastapi/pydantic installed (`.venv/bin/python3`).
"""

import os
import json
import datetime
from unittest.mock import patch

import api
from db import Database, new_id
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator
from scheduler import JobRegistry
from tasks.trading_common import DEFAULT_STRATEGY_PARAMS
from market_data import MarketDataError

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trading_strategy_override.db")


class _FakeHistoricalMarketClient:
    is_mock = False

    def __init__(self, bars_by_symbol, raise_for=()):
        self.bars_by_symbol = bars_by_symbol
        self.raise_for = set(raise_for)

    def get_daily_history(self, symbol, start_date, end_date):
        if symbol in self.raise_for:
            raise MarketDataError(f"no historical data for {symbol}")
        return [b for b in self.bars_by_symbol[symbol] if start_date <= b["date"] <= end_date]


def _bar(date, close):
    return {"date": date, "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1000000, "mock": False}


def _recent_dates(n):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    api.state["db"] = db
    api.state["businesses"] = BusinessRegistry(db)
    api.state["agents"] = AgentRegistry(db)
    api.state["banker"] = Banker(db)
    api.state["approvals"] = ApprovalQueue(db)
    api.state["orchestrator"] = Orchestrator(db, api.state["banker"], api.state["approvals"])
    api.state["jobs"] = JobRegistry(db)

    biz_id = api.state["businesses"].create("Override Test Co", "automated_stock_trading", "test")
    params = {**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL"]}
    db.execute(
        "INSERT INTO trading_strategy_versions (id, business_id, version, parameters, "
        "rationale, source, active) VALUES (?, ?, 1, ?, ?, 'system', 1)",
        (new_id("strat"), biz_id, json.dumps(params), "Initial default strategy parameters."),
    )
    return db, biz_id


def test_override_without_auto_multiples_behaves_exactly_as_before():
    db, biz_id = _setup()
    req = api.TradingStrategyOverrideRequest(
        parameters={**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL"], "stop_loss_pct": 0.1},
        rationale="Manual flat stop.",
    )
    result = api.override_trading_strategy(biz_id, req)
    assert result["parameters"]["stop_loss_pct"] == 0.1
    print("PASS: an override with no ATR multiple set saves the flat % exactly as given, "
          "unchanged from before this feature existed")
    db.close()
    os.remove(TEST_DB_PATH)


def test_override_with_an_atr_multiple_applies_real_volatility_immediately():
    db, biz_id = _setup()
    # Every day's true range is exactly 2.0 against a constant $100 close
    # -> real ATR% = 0.02 -- a 2x multiple must produce exactly 0.04.
    bars = {"AAPL": [_bar(d, 100.0) for d in _recent_dates(6)]}
    fake_market = _FakeHistoricalMarketClient(bars)

    req = api.TradingStrategyOverrideRequest(
        parameters={**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL"], "stop_loss_pct": 0.5,
                    "auto_stop_loss_atr_multiple": 2.0},
        rationale="Owner turned on volatility-based stops.",
    )
    with patch("api.market_data.get_default_client", return_value=fake_market):
        result = api.override_trading_strategy(biz_id, req)

    assert abs(result["parameters"]["stop_loss_pct"] - 0.04) < 1e-9, result["parameters"]["stop_loss_pct"]
    saved = db.query_one(
        "SELECT * FROM trading_strategy_versions WHERE business_id=? AND active=1", (biz_id,),
    )
    assert abs(json.loads(saved["parameters"])["stop_loss_pct"] - 0.04) < 1e-9
    print("PASS: setting an ATR multiple directly via the override endpoint applies real "
          "volatility immediately -- no need to also wait for/trigger a strategy review")
    db.close()
    os.remove(TEST_DB_PATH)


def test_override_with_an_atr_multiple_fails_loudly_when_volatility_unavailable():
    db, biz_id = _setup()
    fake_market = _FakeHistoricalMarketClient({"AAPL": []}, raise_for=["AAPL"])
    req = api.TradingStrategyOverrideRequest(
        parameters={**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL"],
                    "auto_stop_loss_atr_multiple": 2.0},
        rationale="x",
    )
    with patch("api.market_data.get_default_client", return_value=fake_market):
        try:
            api.override_trading_strategy(biz_id, req)
            assert False, "expected an HTTPException"
        except Exception as e:
            assert "no historical data" in str(e) or "no historical data" in str(getattr(e, "detail", ""))

    versions = db.query("SELECT * FROM trading_strategy_versions WHERE business_id=?", (biz_id,))
    assert len(versions) == 1, "a failed ATR computation must never be saved as a new version"
    print("PASS: the override endpoint fails loudly (never saves a new version) when an ATR "
          "multiple is set but real volatility can't be fetched")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_override_without_auto_multiples_behaves_exactly_as_before()
    test_override_with_an_atr_multiple_applies_real_volatility_immediately()
    test_override_with_an_atr_multiple_fails_loudly_when_volatility_unavailable()
    print("\nAll trading strategy override offline tests passed.")
