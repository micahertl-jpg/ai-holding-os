"""
test_backtest_offline.py — real tests for tasks/backtest.py: the
day-by-day simulation loop over historical bars, and the extra stats
(profit_factor, max_drawdown_pct, sample_size_ok) compute_backtest_stats
adds on top of trading_strategy_review.compute_stats(). No real network
access or LLM calls -- a scripted fake LLM client stands in so each
simulated day's decision is exactly controlled, producing a known,
exact trade/stats outcome to assert against.
"""

import json

from tasks.trading_common import DEFAULT_STRATEGY_PARAMS, validate_parameters
from tasks.backtest import (
    run_backtest, compute_backtest_stats, BacktestError, BacktestCancelled,
    MIN_SELL_TRADES_FOR_SIGNAL,
)


class _ScriptedLLMClient:
    """Returns a different canned response each call, in call order --
    lets a test script an exact sequence of days' decisions (buy day 1,
    hold day 2, sell day 3, ...) instead of the same response every
    time."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def complete(self, messages, system=None, max_tokens=1000):
        response = self.responses[self.calls % len(self.responses)]
        self.calls += 1
        return response


def _decision_json(symbol, action, size_pct=0.5, confidence="high", rationale="test"):
    return json.dumps({"decisions": [
        {"symbol": symbol, "action": action, "confidence_level": confidence,
         "size_pct": size_pct, "rationale": rationale}
    ]})


def _bar(date, close, mock=False):
    return {"date": date, "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1000000, "mock": mock}


AAPL_ONLY_PARAMS = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL"]})


def test_run_backtest_simulates_a_buy_then_sell_across_real_days():
    bars = {"AAPL": [
        _bar("2026-01-05", 100.0),
        _bar("2026-01-06", 110.0),
        _bar("2026-01-07", 90.0),
    ]}
    llm = _ScriptedLLMClient([
        _decision_json("AAPL", "buy", size_pct=0.5),
        _decision_json("AAPL", "hold", size_pct=0.0),
        _decision_json("AAPL", "sell", size_pct=1.0),
    ])
    result = run_backtest(1000.0, AAPL_ONLY_PARAMS, bars, llm)

    assert result["days_simulated"] == 3
    executed = result["trades"]
    assert len(executed) == 2, executed
    assert executed[0]["side"] == "buy" and executed[0]["date"] == "2026-01-05"
    assert executed[1]["side"] == "sell" and executed[1]["date"] == "2026-01-07"
    # Bought at $100, sold at $90 -- a real, exact realized loss.
    assert executed[1]["realized_pnl_usd"] == -10.0, executed[1]
    assert len(result["snapshots"]) == 3
    print("PASS: run_backtest simulates a real buy-then-sell sequence across historical days "
          "and records the exact realized P&L")


def test_run_backtest_stops_early_when_should_stop_reports_true():
    """Regression test for the Stop button (POST /tasks/{id}/cancel):
    a 5-day backtest with should_stop flipping to True after the 2nd
    real LLM call must raise BacktestCancelled and NEVER make a 3rd,
    4th, or 5th call -- the whole point of stopping is that a call not
    made never gets billed. A day not yet started never gets billed;
    an in-flight call can't be un-billed, so the check happens BEFORE
    each day's call, never after."""
    bars = {"AAPL": [
        _bar("2026-01-05", 100.0), _bar("2026-01-06", 101.0), _bar("2026-01-07", 102.0),
        _bar("2026-01-08", 103.0), _bar("2026-01-09", 104.0),
    ]}
    llm = _ScriptedLLMClient([_decision_json("AAPL", "hold")] * 5)

    calls_before_stop = {"n": 0}

    def should_stop():
        # Reports "stop" starting on the 3rd check (i.e. once 2 real
        # calls have already happened) -- proves days 3-5 never run.
        return llm.calls >= 2

    try:
        run_backtest(1000.0, AAPL_ONLY_PARAMS, bars, llm, should_stop=should_stop)
        assert False, "expected BacktestCancelled"
    except BacktestCancelled as e:
        assert "2 simulated day" in str(e), str(e)

    assert llm.calls == 2, (
        f"expected exactly 2 real LLM calls before the cancellation was noticed, got {llm.calls} "
        "-- any more means real money was spent on days that should never have run"
    )
    print("PASS: run_backtest stops immediately once should_stop() reports true, making zero "
          "further real LLM calls -- proves a Stop click actually saves money, not just marks "
          "the task cancelled after the fact")


def test_run_backtest_without_should_stop_runs_to_completion_unchanged():
    """should_stop is optional -- every existing caller that doesn't
    pass it (the live smoke-tested paths before this feature existed)
    must behave exactly as before."""
    bars = {"AAPL": [_bar("2026-01-05", 100.0), _bar("2026-01-06", 101.0)]}
    llm = _ScriptedLLMClient([_decision_json("AAPL", "hold")] * 2)
    result = run_backtest(1000.0, AAPL_ONLY_PARAMS, bars, llm)
    assert result["days_simulated"] == 2
    assert llm.calls == 2
    print("PASS: run_backtest with no should_stop given runs to completion exactly as before "
          "this feature existed")


def test_run_backtest_refuses_mock_historical_data():
    bars = {"AAPL": [_bar("2026-01-05", 100.0, mock=True)]}
    llm = _ScriptedLLMClient([_decision_json("AAPL", "hold")])
    try:
        run_backtest(1000.0, AAPL_ONLY_PARAMS, bars, llm)
        assert False, "expected BacktestError"
    except BacktestError as e:
        assert "mock historical data" in str(e)
    print("PASS: run_backtest refuses to run against mock historical data, never produces a "
          "result that looks real but isn't")


def test_run_backtest_raises_with_no_historical_data():
    llm = _ScriptedLLMClient([_decision_json("AAPL", "hold")])
    try:
        run_backtest(1000.0, AAPL_ONLY_PARAMS, {}, llm)
        assert False, "expected BacktestError"
    except BacktestError as e:
        assert "no historical data" in str(e)
    print("PASS: run_backtest raises loudly with no historical data at all")


def test_run_backtest_handles_a_symbol_missing_a_bar_on_some_days():
    # AAPL has no bar on 01-06 (a gap/holiday for it specifically) --
    # MSFT still has one, so that day is NOT skipped entirely, just
    # decided without AAPL in the quotes given to the model.
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL", "MSFT"]})
    bars = {
        "AAPL": [_bar("2026-01-05", 100.0), _bar("2026-01-07", 100.0)],
        "MSFT": [_bar("2026-01-05", 50.0), _bar("2026-01-06", 50.0), _bar("2026-01-07", 50.0)],
    }
    llm = _ScriptedLLMClient([json.dumps({"decisions": [
        {"symbol": s, "action": "hold", "confidence_level": "high", "size_pct": 0.0, "rationale": "x"}
        for s in ["AAPL", "MSFT"]
    ]}), json.dumps({"decisions": [
        {"symbol": "MSFT", "action": "hold", "confidence_level": "high", "size_pct": 0.0, "rationale": "x"}
    ]}), json.dumps({"decisions": [
        {"symbol": s, "action": "hold", "confidence_level": "high", "size_pct": 0.0, "rationale": "x"}
        for s in ["AAPL", "MSFT"]
    ]})])
    result = run_backtest(1000.0, params, bars, llm)
    assert result["days_simulated"] == 3, "the union of trading dates across symbols is used"
    print("PASS: a symbol missing a bar on some days doesn't skip the whole simulated day -- "
          "other symbols with data that day still get decided")


def _sell(pnl, symbol="AAPL"):
    return {"symbol": symbol, "side": "sell", "quantity": 1.0, "price_usd": 100.0,
            "realized_pnl_usd": pnl, "confidence_level": "high", "rationale": "x", "date": "d"}


def test_compute_backtest_stats_computes_profit_factor_and_max_drawdown():
    trades = [_sell(30), _sell(-10)]
    snapshots = [
        {"equity_usd": 1000.0, "cash_usd": 1000.0, "open_positions": 0, "date": "d1"},
        {"equity_usd": 1100.0, "cash_usd": 1100.0, "open_positions": 0, "date": "d2"},  # peak
        {"equity_usd": 990.0, "cash_usd": 990.0, "open_positions": 0, "date": "d3"},   # drawdown from peak
    ]
    stats = compute_backtest_stats(trades, snapshots)
    assert stats["net_pnl_usd"] == 20.0
    assert stats["profit_factor"] == 3.0  # 30 gross win / 10 gross loss
    assert abs(stats["max_drawdown_pct"] - (1100.0 - 990.0) / 1100.0) < 1e-9
    print("PASS: compute_backtest_stats computes profit_factor and max_drawdown_pct correctly "
          "from a real trade/equity sequence")


def test_compute_backtest_stats_flags_a_thin_sample_size():
    trades = [_sell(10)]  # just one completed trade
    snapshots = [{"equity_usd": 1010.0, "cash_usd": 1010.0, "open_positions": 0, "date": "d1"}]
    stats = compute_backtest_stats(trades, snapshots)
    assert stats["sell_trades"] < MIN_SELL_TRADES_FOR_SIGNAL
    assert stats["sample_size_ok"] is False
    print("PASS: compute_backtest_stats flags a thin sample size rather than presenting it as "
          "trustworthy")


def test_compute_backtest_stats_treats_zero_losses_as_infinite_profit_factor():
    trades = [_sell(10), _sell(20)]
    snapshots = [{"equity_usd": 1030.0, "cash_usd": 1030.0, "open_positions": 0, "date": "d1"}]
    stats = compute_backtest_stats(trades, snapshots)
    assert stats["profit_factor"] == float("inf")
    print("PASS: a strategy with zero losing trades gets an infinite profit_factor, not a "
          "division error")


def test_compute_backtest_stats_handles_no_trades_at_all():
    stats = compute_backtest_stats([], [])
    assert stats["profit_factor"] is None
    assert stats["max_drawdown_pct"] is None
    assert stats["sample_size_ok"] is False
    print("PASS: compute_backtest_stats degrades cleanly with zero trades/snapshots, never "
          "divides by zero or fabricates a number")


if __name__ == "__main__":
    test_run_backtest_simulates_a_buy_then_sell_across_real_days()
    test_run_backtest_stops_early_when_should_stop_reports_true()
    test_run_backtest_without_should_stop_runs_to_completion_unchanged()
    test_run_backtest_refuses_mock_historical_data()
    test_run_backtest_raises_with_no_historical_data()
    test_run_backtest_handles_a_symbol_missing_a_bar_on_some_days()
    test_compute_backtest_stats_computes_profit_factor_and_max_drawdown()
    test_compute_backtest_stats_flags_a_thin_sample_size()
    test_compute_backtest_stats_treats_zero_losses_as_infinite_profit_factor()
    test_compute_backtest_stats_handles_no_trades_at_all()
    print("\nAll backtest.py offline tests passed.")
