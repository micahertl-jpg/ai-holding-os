"""
test_trading_strategy_review_offline.py — real tests for
tasks/trading_strategy_review.py: the pure stats computation and the
model-proposal validation path (including the case that matters most --
a proposal that violates trading_common's bounds must be REJECTED, never
saved as if it were valid).
"""

import json

from tasks.trading_common import DEFAULT_STRATEGY_PARAMS
from tasks.trading_strategy_review import (
    compute_stats, propose_strategy_update, TradingStrategyReviewError,
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


if __name__ == "__main__":
    test_compute_stats_on_empty_history()
    test_compute_stats_win_rate_and_realized_pnl()
    test_compute_stats_drawdown_from_snapshots()
    test_propose_strategy_update_accepts_a_valid_conservative_proposal()
    test_propose_strategy_update_rejects_out_of_bounds_parameters()
    test_propose_strategy_update_rejects_missing_top_level_field()
    test_propose_strategy_update_rejects_non_json()
    test_propose_strategy_update_rejects_invalid_confidence_level()
    print("\nAll trading_strategy_review.py offline tests passed.")
