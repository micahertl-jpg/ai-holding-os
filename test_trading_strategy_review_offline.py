"""
test_trading_strategy_review_offline.py — real tests for
tasks/trading_strategy_review.py: the pure stats computation and the
model-proposal validation path (including the case that matters most --
a proposal that violates trading_common's bounds must be REJECTED, never
saved as if it were valid).
"""

import json

from tasks.trading_common import DEFAULT_STRATEGY_PARAMS, ABSOLUTE_MAX_STOP_LOSS_PCT
from tasks.trading_strategy_review import (
    compute_stats, propose_strategy_update, apply_auto_volatility_stops, TradingStrategyReviewError,
)


class _FakeLLMClient:
    def __init__(self, canned_response):
        self.canned_response = canned_response
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def complete(self, messages, system=None, max_tokens=1000):
        return self.canned_response


def _valid_proposal_json(**overrides):
    params = dict(DEFAULT_STRATEGY_PARAMS)
    params.update(overrides)
    return json.dumps({
        "parameters": params,
        "rationale": "Small, conservative tightening given a thin trade history.",
        "confidence_level": "medium",
    })


# ---------------------------------------------------------------------
# compute_stats — pure arithmetic, no LLM/DB
# ---------------------------------------------------------------------

def test_compute_stats_on_empty_history():
    stats = compute_stats([], [])
    assert stats["total_trades"] == 0
    assert stats["win_rate"] is None
    assert stats["current_equity_usd"] is None
    print("PASS: compute_stats handles a portfolio with no trades/snapshots yet without erroring")


def test_compute_stats_win_rate_and_realized_pnl():
    trades = [
        {"side": "buy", "realized_pnl_usd": None},
        {"side": "sell", "realized_pnl_usd": 50.0},
        {"side": "sell", "realized_pnl_usd": -20.0},
        {"side": "sell", "realized_pnl_usd": 30.0},
    ]
    stats = compute_stats(trades, [])
    assert stats["total_trades"] == 4
    assert stats["sell_trades"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert abs(stats["win_rate"] - (2 / 3)) < 1e-9
    assert abs(stats["total_realized_pnl_usd"] - 60.0) < 1e-9
    print("PASS: compute_stats correctly computes win rate and total realized P&L from "
          "real trade rows, only counting sell (completed) trades")


def test_compute_stats_drawdown_from_snapshots():
    snapshots = [{"equity_usd": 10000}, {"equity_usd": 11000}, {"equity_usd": 9900}]
    stats = compute_stats([], snapshots)
    assert stats["peak_equity_usd"] == 11000
    assert stats["current_equity_usd"] == 9900
    assert abs(stats["current_drawdown_pct"] - (1100 / 11000)) < 1e-9
    print("PASS: compute_stats computes current drawdown from the real peak equity, "
          "not just the most recent value")


# ---------------------------------------------------------------------
# propose_strategy_update — model output validation
# ---------------------------------------------------------------------

def test_propose_strategy_update_accepts_a_valid_conservative_proposal():
    client = _FakeLLMClient(_valid_proposal_json(max_position_pct=0.10))
    stats = compute_stats([], [])
    result = propose_strategy_update(DEFAULT_STRATEGY_PARAMS, stats, "", client)
    assert result["parameters"]["max_position_pct"] == 0.10
    assert result["confidence_level"] == "medium"
    print("PASS: propose_strategy_update accepts a valid, in-bounds proposal")


def test_propose_strategy_update_rejects_out_of_bounds_parameters():
    # Model tries to raise max_position_pct past the absolute ceiling —
    # this must be rejected outright, never clamped and silently saved.
    client = _FakeLLMClient(_valid_proposal_json(max_position_pct=0.95))
    stats = compute_stats([], [])
    try:
        propose_strategy_update(DEFAULT_STRATEGY_PARAMS, stats, "", client)
        assert False, "expected TradingStrategyReviewError"
    except TradingStrategyReviewError as e:
        assert "rejected" in str(e)
    print("PASS: propose_strategy_update REJECTS a proposal whose parameters exceed the "
          "absolute ceilings -- a bad review can never sneak a looser risk limit through")


def test_propose_strategy_update_rejects_missing_top_level_field():
    client = _FakeLLMClient(json.dumps({"parameters": dict(DEFAULT_STRATEGY_PARAMS),
                                         "confidence_level": "medium"}))  # no "rationale"
    stats = compute_stats([], [])
    try:
        propose_strategy_update(DEFAULT_STRATEGY_PARAMS, stats, "", client)
        assert False, "expected TradingStrategyReviewError"
    except TradingStrategyReviewError as e:
        assert "rationale" in str(e)
    print("PASS: propose_strategy_update rejects a response missing rationale")


def test_propose_strategy_update_rejects_non_json():
    client = _FakeLLMClient("not json")
    stats = compute_stats([], [])
    try:
        propose_strategy_update(DEFAULT_STRATEGY_PARAMS, stats, "", client)
        assert False, "expected TradingStrategyReviewError"
    except TradingStrategyReviewError:
        pass
    print("PASS: propose_strategy_update fails loudly on non-JSON model output")


def test_propose_strategy_update_rejects_invalid_confidence_level():
    client = _FakeLLMClient(json.dumps({
        "parameters": dict(DEFAULT_STRATEGY_PARAMS),
        "rationale": "x",
        "confidence_level": "extreme",
    }))
    stats = compute_stats([], [])
    try:
        propose_strategy_update(DEFAULT_STRATEGY_PARAMS, stats, "", client)
        assert False, "expected TradingStrategyReviewError"
    except TradingStrategyReviewError:
        pass
    print("PASS: propose_strategy_update rejects an invalid top-level confidence_level")


# ---------------------------------------------------------------------
# apply_auto_volatility_stops — the automatic (ATR-based) stop-loss/
# take-profit: real volatility, computed in code, overriding whatever
# flat stop_loss_pct/take_profit_pct the model (or the owner) proposed
# ---------------------------------------------------------------------

def _atr_bar(date, high, low, close):
    return {"date": date, "open": close, "high": high, "low": low, "close": close,
            "volume": 1000000, "mock": False}


def test_apply_auto_volatility_stops_overrides_the_flat_percentages():
    # Every day's true range is exactly 2.0 against a $100 close -> real
    # ATR% = 0.02. A 2x stop-loss multiple and 3x take-profit multiple
    # must produce exactly 0.04 and 0.06, NOT whatever flat stop_loss_pct/
    # take_profit_pct the params dict also happens to carry.
    bars = {"AAPL": [_atr_bar(f"2026-01-0{i}", 101, 99, 100) for i in range(1, 7)]}
    params = {**DEFAULT_STRATEGY_PARAMS, "stop_loss_pct": 0.5, "take_profit_pct": 0.5,
              "auto_stop_loss_atr_multiple": 2.0, "auto_take_profit_atr_multiple": 3.0}
    result = apply_auto_volatility_stops(params, bars)
    assert abs(result["stop_loss_pct"] - 0.04) < 1e-9, result["stop_loss_pct"]
    assert abs(result["take_profit_pct"] - 0.06) < 1e-9, result["take_profit_pct"]
    print("PASS: apply_auto_volatility_stops overrides stop_loss_pct/take_profit_pct with "
          "values computed from real volatility, ignoring whatever flat % was also set")


def test_apply_auto_volatility_stops_is_a_no_op_when_neither_multiple_is_set():
    params = dict(DEFAULT_STRATEGY_PARAMS)
    params["stop_loss_pct"] = 0.08
    result = apply_auto_volatility_stops(params, {})
    assert result["stop_loss_pct"] == 0.08
    print("PASS: apply_auto_volatility_stops leaves stop_loss_pct/take_profit_pct exactly "
          "as given when no ATR multiple is configured, never touching the network/bars")


def test_apply_auto_volatility_stops_only_overrides_the_one_multiple_thats_set():
    bars = {"AAPL": [_atr_bar(f"2026-01-0{i}", 101, 99, 100) for i in range(1, 7)]}
    params = {**DEFAULT_STRATEGY_PARAMS, "stop_loss_pct": 0.5, "take_profit_pct": 0.5,
              "auto_stop_loss_atr_multiple": 2.0, "auto_take_profit_atr_multiple": None}
    result = apply_auto_volatility_stops(params, bars)
    assert abs(result["stop_loss_pct"] - 0.04) < 1e-9
    assert result["take_profit_pct"] == 0.5, "take-profit is untouched -- its own multiple is unset"
    print("PASS: apply_auto_volatility_stops only recomputes the field whose own multiple is set")


def test_apply_auto_volatility_stops_clamps_to_the_absolute_ceiling():
    # A huge multiple x this volatility would exceed ABSOLUTE_MAX_STOP_LOSS_PCT
    # (0.5) -- must clamp, never silently save an unbounded stop.
    bars = {"AAPL": [_atr_bar(f"2026-01-0{i}", 120, 80, 100) for i in range(1, 7)]}  # ATR%=0.4
    params = {**DEFAULT_STRATEGY_PARAMS, "auto_stop_loss_atr_multiple": 10.0,
              "auto_take_profit_atr_multiple": None}
    result = apply_auto_volatility_stops(params, bars)
    assert result["stop_loss_pct"] == ABSOLUTE_MAX_STOP_LOSS_PCT
    print("PASS: apply_auto_volatility_stops clamps a computed stop to the same absolute "
          "ceiling a manually-set one is bound by, never exceeding it")


def test_apply_auto_volatility_stops_raises_when_volatility_cannot_be_computed():
    params = {**DEFAULT_STRATEGY_PARAMS, "auto_stop_loss_atr_multiple": 2.0}
    try:
        apply_auto_volatility_stops(params, {"AAPL": [_atr_bar("2026-01-01", 101, 99, 100)]})
        assert False, "expected TradingStrategyReviewError"
    except TradingStrategyReviewError as e:
        assert "volatility" in str(e)
    print("PASS: apply_auto_volatility_stops refuses loudly rather than silently keeping a "
          "stale/arbitrary value when real volatility can't be computed")


def test_apply_auto_volatility_stops_never_mutates_the_input_dict():
    bars = {"AAPL": [_atr_bar(f"2026-01-0{i}", 101, 99, 100) for i in range(1, 7)]}
    params = {**DEFAULT_STRATEGY_PARAMS, "auto_stop_loss_atr_multiple": 2.0, "stop_loss_pct": 0.5}
    original = dict(params)
    apply_auto_volatility_stops(params, bars)
    assert params == original, "the caller's own dict must never be mutated in place"
    print("PASS: apply_auto_volatility_stops returns a new dict, never mutates its input")


if __name__ == "__main__":
    test_compute_stats_on_empty_history()
    test_compute_stats_win_rate_and_realized_pnl()
    test_compute_stats_drawdown_from_snapshots()
    test_propose_strategy_update_accepts_a_valid_conservative_proposal()
    test_propose_strategy_update_rejects_out_of_bounds_parameters()
    test_propose_strategy_update_rejects_missing_top_level_field()
    test_propose_strategy_update_rejects_non_json()
    test_propose_strategy_update_rejects_invalid_confidence_level()
    test_apply_auto_volatility_stops_overrides_the_flat_percentages()
    test_apply_auto_volatility_stops_is_a_no_op_when_neither_multiple_is_set()
    test_apply_auto_volatility_stops_only_overrides_the_one_multiple_thats_set()
    test_apply_auto_volatility_stops_clamps_to_the_absolute_ceiling()
    test_apply_auto_volatility_stops_raises_when_volatility_cannot_be_computed()
    test_apply_auto_volatility_stops_never_mutates_the_input_dict()
    print("\nAll trading_strategy_review.py offline tests passed.")
