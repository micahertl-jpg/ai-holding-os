"""
tasks/strategy_backtest_search.py — a BOUNDED, train/validation-checked
search for a better strategy, run against real historical data via
tasks/backtest.py. This exists specifically to avoid the failure mode
of "keep tweaking parameters until some metric crosses a target": that
approach overfits to whatever historical window it's tuned against and
produces a strategy that looks great on the data it was shaped by and
falls apart on anything new -- exactly the data a real live account
would then be exposed to.

Two things keep this honest:
1. TRAIN / VALIDATION SPLIT. Every candidate strategy is backtested on
   a TRAIN window (what the model sees when proposing changes) and
   separately on a VALIDATION window it was never shown or tuned
   against. `meets_bar()` -- the actual pass/fail check -- is only ever
   evaluated against VALIDATION stats. A candidate that looks great on
   train but falls apart on validation is exactly the overfit case this
   is designed to catch, and it's surfaced as such (train_meets_bar vs
   validation_meets_bar), never silently hidden.
2. BOUNDED ITERATION. `run_strategy_search()` tries at most
   `max_candidates` strategies (default 5) and then stops and reports
   the best one found by real net profitability -- never an open-ended
   loop that keeps trying until an arbitrary number is hit. "None of
   these met the bar" is a legitimate, expected outcome, not a bug to
   route around.

The pass/fail bar itself (meets_bar) deliberately never uses win_rate
alone: net P&L, profit_factor (the real tell for whether a high win
rate is actually making money), a minimum completed-trade sample size,
and a drawdown ceiling all have to hold. See tasks/backtest.py's
compute_backtest_stats() for where these numbers come from.

This module never activates anything -- it only returns candidate
strategies with their real train/validation stats. Promoting a
candidate to the business's active strategy (paper or, later, live) is
always a separate, explicit owner action (the existing
POST /businesses/{id}/trading/strategy-override endpoint), same as any
other strategy change in this codebase.
"""

from tasks.backtest import run_backtest, compute_backtest_stats, BacktestError
from tasks.trading_strategy_review import propose_strategy_update, TradingStrategyReviewError

DEFAULT_MAX_CANDIDATES = 5

# The actual pass bar, checked ONLY against validation (held-out) stats.
MIN_PROFIT_FACTOR = 1.2
MAX_ALLOWED_DRAWDOWN_PCT = 0.20


class StrategySearchError(Exception):
    pass


def split_bars_by_date(bars_by_symbol: dict, split_date: str) -> tuple:
    """Partitions each symbol's historical bars into (train, validation)
    by date: bars with date < split_date go to train, bars with
    date >= split_date go to validation. Pure, no I/O -- the caller
    fetches one contiguous historical range and this cuts it, so a
    fetched range never accidentally leaks train-period data into the
    validation set or vice versa."""
    train, validation = {}, {}
    for symbol, bars in bars_by_symbol.items():
        train[symbol] = [b for b in bars if b["date"] < split_date]
        validation[symbol] = [b for b in bars if b["date"] >= split_date]
    return train, validation


def meets_bar(stats: dict, min_profit_factor: float = MIN_PROFIT_FACTOR,
              max_allowed_drawdown_pct: float = MAX_ALLOWED_DRAWDOWN_PCT) -> bool:
    """A strategy is only 'good' if: there are enough completed trades
    to trust the numbers (sample_size_ok), it's net profitable in real
    dollars, its profit_factor clears a real margin above break-even
    (1.0) -- not just a positive win rate -- and its drawdown stayed
    under a sane ceiling. win_rate is never part of this check on its
    own; see this module's docstring for why."""
    if not stats.get("sample_size_ok"):
        return False
    net_pnl = stats.get("net_pnl_usd")
    if net_pnl is None or net_pnl <= 0:
        return False
    profit_factor = stats.get("profit_factor")
    if profit_factor is None or profit_factor < min_profit_factor:
        return False
    max_dd = stats.get("max_drawdown_pct")
    if max_dd is not None and max_dd > max_allowed_drawdown_pct:
        return False
    return True


def _recent_trades_summary(trades: list, n: int = 10) -> str:
    recent = trades[-n:]
    lines = [
        f"- {t['side']} {t['quantity']:.4f} {t['symbol']} @ ${t['price_usd']:.2f}"
        + (f" (realized P&L ${t['realized_pnl_usd']:+.2f})"
           if t["side"] == "sell" and t["realized_pnl_usd"] is not None else "")
        for t in recent
    ]
    return "\n".join(lines) if lines else "(no trades yet)"


def _evaluate_candidate(starting_cash_usd, params, train_bars, validation_bars, llm_client):
    """Backtests one candidate's parameters on both windows. Raises
    BacktestError up through to the caller unchanged -- a candidate
    that can't even be backtested (e.g. a data gap) is a real failure,
    never silently skipped."""
    train_result = run_backtest(starting_cash_usd, params, train_bars, llm_client)
    train_stats = compute_backtest_stats(train_result["trades"], train_result["snapshots"])

    validation_result = run_backtest(starting_cash_usd, params, validation_bars, llm_client)
    validation_stats = compute_backtest_stats(validation_result["trades"], validation_result["snapshots"])

    return {
        "parameters": params,
        "train_stats": train_stats,
        "validation_stats": validation_stats,
        "train_meets_bar": meets_bar(train_stats),
        "validation_meets_bar": meets_bar(validation_stats),
        "train_trades": train_result["trades"],
        "validation_trades": validation_result["trades"],
    }


def _candidate_rank_key(candidate):
    """Ranks candidates by real, out-of-sample (validation) net
    profitability -- never by train performance (which a proposal was
    directly tuned against) and never by win rate alone."""
    net_pnl = candidate["validation_stats"].get("net_pnl_usd")
    return net_pnl if net_pnl is not None else float("-inf")


def run_strategy_search(starting_cash_usd, current_params, train_bars, validation_bars,
                         llm_client, max_candidates: int = DEFAULT_MAX_CANDIDATES) -> dict:
    """Evaluates the current strategy, then proposes and backtests up to
    (max_candidates - 1) further candidates via
    trading_strategy_review.propose_strategy_update() -- reusing that
    exact, already-bounds-checked proposal path, fed REAL backtest stats
    instead of live/paper stats. Stops early the first time a
    candidate's VALIDATION stats clear meets_bar(). Returns
    {"candidates": [...], "best_index": int|None, "stopped_early": bool}
    -- candidates[best_index] is the best by real out-of-sample net
    profitability among everything tried, which may or may not clear
    the bar; "none of these are good enough yet" is reported plainly,
    never disguised as a pass.

    Raises StrategySearchError if the very first (current-parameters)
    candidate can't be backtested at all (e.g. no real historical data)
    -- there's nothing meaningful to report in that case."""
    if max_candidates < 1:
        raise StrategySearchError("max_candidates must be at least 1")

    try:
        candidates = [_evaluate_candidate(starting_cash_usd, current_params, train_bars,
                                           validation_bars, llm_client)]
    except BacktestError as e:
        raise StrategySearchError(f"could not backtest the current strategy: {e}") from e

    stopped_early = candidates[0]["validation_meets_bar"]

    while not stopped_early and len(candidates) < max_candidates:
        basis = candidates[-1]
        try:
            proposal = propose_strategy_update(
                basis["parameters"], basis["train_stats"],
                _recent_trades_summary(basis["train_trades"]), llm_client,
            )
        except TradingStrategyReviewError:
            # A malformed/out-of-bounds proposal ends the search here --
            # report what was found so far rather than failing the
            # whole search over one bad proposal.
            break

        try:
            candidate = _evaluate_candidate(starting_cash_usd, proposal["parameters"],
                                             train_bars, validation_bars, llm_client)
        except BacktestError:
            break
        candidate["rationale"] = proposal["rationale"]
        candidate["confidence_level"] = proposal["confidence_level"]
        candidates.append(candidate)
        stopped_early = candidate["validation_meets_bar"]

    best_index = max(range(len(candidates)), key=lambda i: _candidate_rank_key(candidates[i]))

    return {"candidates": candidates, "best_index": best_index, "stopped_early": stopped_early}
