"""
test_strategy_backtest_search_offline.py — real tests for
tasks/strategy_backtest_search.py: the bounded, train/validation-checked
strategy search. The core claims under test: the pass bar (meets_bar)
never uses win_rate alone, candidates are ranked by real out-of-sample
(validation) net profitability rather than train performance, the
search stops the moment a candidate clears the bar, and it never
exceeds max_candidates even when nothing ever passes.
"""

import json
from unittest.mock import patch

from tasks.trading_common import DEFAULT_STRATEGY_PARAMS, validate_parameters
from tasks.strategy_backtest_search import (
    split_bars_by_date, meets_bar, run_strategy_search, StrategySearchError,
)


class _MultiPurposeLLMClient:
    """Routes each call to a decision response or a proposal response
    based on which system prompt it's given -- lets one fake client
    serve both run_backtest's decide_trades() calls (system prompt
    contains "paper-trading equity analyst") and
    propose_strategy_update()'s calls (system prompt contains
    "reviewing the performance") within the same search, each with its
    own exact scripted call sequence."""

    def __init__(self, decision_responses, proposal_responses=()):
        self.decision_responses = list(decision_responses)
        self.proposal_responses = list(proposal_responses)
        self.decision_calls = 0
        self.proposal_calls = 0
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def complete(self, messages, system=None, max_tokens=1000):
        if system and "paper-trading equity analyst" in system:
            response = self.decision_responses[self.decision_calls % len(self.decision_responses)]
            self.decision_calls += 1
            return response
        response = self.proposal_responses[self.proposal_calls % len(self.proposal_responses)]
        self.proposal_calls += 1
        return response


def _bar(date, close, mock=False):
    return {"date": date, "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1000000, "mock": mock}


def _decisions(*entries):
    """entries: list of (symbol, action, size_pct)."""
    return json.dumps({"decisions": [
        {"symbol": s, "action": a, "confidence_level": "high", "size_pct": p, "rationale": "x"}
        for s, a, p in entries
    ]})


AAPL_ONLY_PARAMS = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL"]})

_PROPOSAL_JSON = json.dumps({
    "parameters": {**AAPL_ONLY_PARAMS, "drawdown_halt_pct": 0.10},
    "rationale": "Tightened the drawdown halt after a weak train-window result.",
    "confidence_level": "medium",
})


def test_split_bars_by_date_partitions_correctly():
    bars = {"AAPL": [_bar("2026-01-01", 100), _bar("2026-01-05", 101), _bar("2026-01-10", 102)]}
    train, validation = split_bars_by_date(bars, "2026-01-05")
    assert [b["date"] for b in train["AAPL"]] == ["2026-01-01"]
    assert [b["date"] for b in validation["AAPL"]] == ["2026-01-05", "2026-01-10"]
    print("PASS: split_bars_by_date partitions bars strictly before/on-or-after the split date")


def _stats(net_pnl=10.0, profit_factor=2.0, max_dd=0.05, sample_ok=True):
    return {"net_pnl_usd": net_pnl, "profit_factor": profit_factor,
            "max_drawdown_pct": max_dd, "sample_size_ok": sample_ok}


def test_meets_bar_requires_all_four_conditions():
    assert meets_bar(_stats()) is True
    assert meets_bar(_stats(sample_ok=False)) is False, "a thin sample must never pass"
    assert meets_bar(_stats(net_pnl=0.0)) is False, "break-even is not a pass"
    assert meets_bar(_stats(net_pnl=-1.0)) is False
    assert meets_bar(_stats(profit_factor=1.19)) is False, "just under the profit_factor floor"
    assert meets_bar(_stats(profit_factor=1.2)) is True, "exactly at the profit_factor floor"
    assert meets_bar(_stats(max_dd=0.21)) is False, "just over the drawdown ceiling"
    assert meets_bar(_stats(max_dd=0.20)) is True, "exactly at the drawdown ceiling"
    print("PASS: meets_bar requires sample size, positive net P&L, a real profit_factor "
          "margin, and a bounded drawdown -- never win_rate alone (not even in the stats dict)")


def test_run_strategy_search_stops_early_when_current_strategy_already_meets_the_bar():
    bars = {"AAPL": [_bar("2026-01-05", 100.0), _bar("2026-01-06", 120.0)]}
    llm = _MultiPurposeLLMClient(decision_responses=[
        _decisions(("AAPL", "buy", 0.5)),
        _decisions(("AAPL", "sell", 1.0)),
    ])
    with patch("tasks.backtest.MIN_SELL_TRADES_FOR_SIGNAL", 1):
        result = run_strategy_search(1000.0, AAPL_ONLY_PARAMS, bars, bars, llm, max_candidates=5)

    assert result["stopped_early"] is True
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["validation_meets_bar"] is True
    assert result["best_index"] == 0
    print("PASS: the search stops immediately when the CURRENT strategy already clears the "
          "bar, never proposing changes it doesn't need")


def test_run_strategy_search_never_exceeds_max_candidates_when_nothing_passes():
    bars = {"AAPL": [_bar("2026-01-05", 100.0), _bar("2026-01-06", 100.0)]}
    hold_only = _decisions(("AAPL", "hold", 0.0))
    llm = _MultiPurposeLLMClient(
        decision_responses=[hold_only],  # every day, every candidate: hold -- zero trades ever
        proposal_responses=[_PROPOSAL_JSON],
    )
    with patch("tasks.backtest.MIN_SELL_TRADES_FOR_SIGNAL", 1):
        result = run_strategy_search(1000.0, AAPL_ONLY_PARAMS, bars, bars, llm, max_candidates=3)

    assert len(result["candidates"]) == 3, "must stop at max_candidates, never loop forever"
    assert result["stopped_early"] is False
    assert all(c["validation_meets_bar"] is False for c in result["candidates"])
    print("PASS: when no candidate ever clears the bar, the search stops at max_candidates "
          "and reports failure plainly rather than looping indefinitely")


def test_run_strategy_search_ranks_by_validation_net_pnl_not_train_or_win_rate():
    train_bars = {"AAPL": [_bar("2026-01-01", 100.0)]}
    validation_bars = {"AAPL": [_bar("2026-01-05", 100.0), _bar("2026-01-06", 90.0)]}
    hold = _decisions(("AAPL", "hold", 0.0))
    buy = _decisions(("AAPL", "buy", 0.5))
    sell = _decisions(("AAPL", "sell", 1.0))
    llm = _MultiPurposeLLMClient(
        decision_responses=[
            hold,          # candidate 0 train day (hold)
            buy, sell,     # candidate 0 validation: buys at 100, sells at 90 -- a real loss
            hold,          # candidate 1 train day (hold)
            hold, hold,    # candidate 1 validation: holds both days -- net_pnl 0.0, no loss
        ],
        proposal_responses=[_PROPOSAL_JSON],
    )
    with patch("tasks.backtest.MIN_SELL_TRADES_FOR_SIGNAL", 1):
        result = run_strategy_search(1000.0, AAPL_ONLY_PARAMS, train_bars, validation_bars,
                                      llm, max_candidates=2)

    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["validation_stats"]["net_pnl_usd"] == -10.0
    assert result["candidates"][1]["validation_stats"]["net_pnl_usd"] == 0.0
    assert result["best_index"] == 1, "0.0 beats -10.0 -- ranked on real validation P&L"
    print("PASS: candidates are ranked by real, out-of-sample validation net P&L -- a "
          "losing candidate never outranks a flat one just because it traded more")


def test_run_strategy_search_raises_if_the_current_strategy_cannot_be_backtested():
    llm = _MultiPurposeLLMClient(decision_responses=[_decisions(("AAPL", "hold", 0.0))])
    try:
        run_strategy_search(1000.0, AAPL_ONLY_PARAMS, {}, {}, llm)
        assert False, "expected StrategySearchError"
    except StrategySearchError as e:
        assert "could not backtest" in str(e)
    print("PASS: the search fails loudly if even the current strategy can't be backtested "
          "(e.g. no historical data), rather than reporting an empty/fabricated result")


if __name__ == "__main__":
    test_split_bars_by_date_partitions_correctly()
    test_meets_bar_requires_all_four_conditions()
    test_run_strategy_search_stops_early_when_current_strategy_already_meets_the_bar()
    test_run_strategy_search_never_exceeds_max_candidates_when_nothing_passes()
    test_run_strategy_search_ranks_by_validation_net_pnl_not_train_or_win_rate()
    test_run_strategy_search_raises_if_the_current_strategy_cannot_be_backtested()
    print("\nAll strategy_backtest_search.py offline tests passed.")
