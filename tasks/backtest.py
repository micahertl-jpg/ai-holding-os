"""
tasks/backtest.py — tests a strategy against REAL historical price data
instead of live quotes, so a strategy can be evaluated over months of
real past market action without waiting that much real time. Reuses
tasks/trading_cycle.py's decide_trades()/apply_risk_limits() completely
unchanged -- a backtest is "what this strategy would have really
decided," not a separate simulation with its own rules, so a result
found here means something for the real (paper or live) thing.

Runs day-by-day over historical daily bars: each day's close price
stands in as the price the model traded at, apply_risk_limits() clamps
exactly as it would in a real cycle, and trades/snapshots are recorded
in the same shape tasks/trading_strategy_review.py's compute_stats()
already expects from paper_trades/trading_snapshots -- so backtest
stats and live/paper stats are directly comparable with no separate
stats implementation, and every dashboard renderer that already knows
how to show a trade/snapshot works unchanged on backtest output.

Cost note, stated explicitly rather than hidden: each simulated day is
one real LLM call (same as one real trading_cycle task), so backtesting
N days costs roughly N times what a single live cycle costs -- real
USD either way. Keep date ranges reasonable.

Refuses to run against ANY mock historical bar -- same "never trade on
fabricated prices" rule live/paper trading already enforces (see
tasks/trading_cycle.py's run_trading_cycle) -- a backtest result that
looked good against fabricated data would be worthless and actively
dangerous to trust when deciding whether to risk real capital.
"""

from tasks.trading_cycle import decide_trades, apply_risk_limits, TradingCycleError
from tasks.trading_strategy_review import compute_stats

# Below this many completed (sell) trades, a backtest's stats are
# treated as too thin a sample to trust -- flagged via sample_size_ok,
# never silently hidden. 20 completed trades is still a small sample
# for real statistical confidence, but is a reasonable floor for an
# MVP; a strategy with fewer than this simply hasn't traded enough
# during the backtest window to say anything meaningful yet.
MIN_SELL_TRADES_FOR_SIGNAL = 20


class BacktestError(Exception):
    pass


def _bars_to_daily_quotes(bars_by_symbol_by_date, date):
    """For one simulated day, builds the quotes dict decide_trades()/
    apply_risk_limits() expect: {symbol: {"symbol","price","as_of","mock"}}
    -- only for symbols that actually have a bar on this date (a holiday,
    a late IPO, or a data gap simply drops that symbol for that day,
    same as a live quote-fetch failure would)."""
    quotes = {}
    for symbol, bars_by_date in bars_by_symbol_by_date.items():
        bar = bars_by_date.get(date)
        if bar is None:
            continue
        quotes[symbol] = {"symbol": symbol, "price": bar["close"], "as_of": date, "mock": False}
    return quotes


def run_backtest(starting_cash_usd, strategy_params, historical_bars_by_symbol, llm_client,
                  max_days=None) -> dict:
    """Steps day-by-day through the union of trading dates present
    across historical_bars_by_symbol (oldest first, optionally capped
    at max_days), running the exact same decide/apply_risk_limits pair
    as a real trading cycle each day. Returns {"trades", "snapshots",
    "days_simulated", "starting_cash_usd"}; trades/snapshots are in the
    same field shape as paper_trades/trading_snapshots rows (plus a
    "date" field), so compute_backtest_stats() below (and anything that
    already knows how to read a paper_trades row) works unchanged.

    Raises BacktestError up front if given ANY mock historical bar, or
    if a simulated day's model call fails structurally (never silently
    skips a bad day and keeps going -- a partial backtest that hid a
    failure would misrepresent the strategy's real performance)."""
    if not historical_bars_by_symbol:
        raise BacktestError("no historical data provided -- nothing to backtest against")

    bars_by_symbol_by_date = {}
    all_dates = set()
    for symbol, bars in historical_bars_by_symbol.items():
        mocked = [b for b in bars if b.get("mock")]
        if mocked:
            raise BacktestError(
                f"refusing to backtest -- mock historical data for {symbol} "
                f"(ALPHAVANTAGE_API_KEY not configured; see README ACTION REQUIRED)"
            )
        bars_by_symbol_by_date[symbol] = {b["date"]: b for b in bars}
        all_dates.update(b["date"] for b in bars)

    sorted_dates = sorted(all_dates)
    if max_days is not None:
        sorted_dates = sorted_dates[:max_days]
    if not sorted_dates:
        raise BacktestError("no trading days found in the given historical data")

    cash = starting_cash_usd
    positions_by_symbol = {}
    trades = []
    snapshots = []
    recent_trades_summary = "(no trades yet)"

    for date in sorted_dates:
        quotes = _bars_to_daily_quotes(bars_by_symbol_by_date, date)
        if not quotes:
            continue  # no symbol has a bar this date (holiday/gap) -- skip the day entirely

        current_positions = list(positions_by_symbol.values())
        try:
            raw_decisions = decide_trades(cash, current_positions, strategy_params,
                                           recent_trades_summary, quotes, llm_client)
        except TradingCycleError as e:
            raise BacktestError(f"backtest failed on simulated day {date}: {e}") from e
        day_trades = apply_risk_limits(raw_decisions, cash, current_positions, quotes, strategy_params)

        for t in day_trades:
            if not t["executed"]:
                continue
            existing = positions_by_symbol.get(
                t["symbol"], {"symbol": t["symbol"], "quantity": 0.0, "avg_cost_usd": 0.0}
            )
            realized_pnl = None
            if t["side"] == "buy":
                new_qty = existing["quantity"] + t["quantity"]
                existing["avg_cost_usd"] = (
                    (existing["quantity"] * existing["avg_cost_usd"] + t["quantity"] * t["price"]) / new_qty
                    if new_qty > 0 else 0.0
                )
                existing["quantity"] = new_qty
                cash -= t["quantity"] * t["price"]
            else:  # sell
                realized_pnl = (t["price"] - existing["avg_cost_usd"]) * t["quantity"]
                existing["quantity"] -= t["quantity"]
                if existing["quantity"] <= 1e-9:
                    existing["quantity"] = 0.0
                    existing["avg_cost_usd"] = 0.0
                cash += t["quantity"] * t["price"]
            positions_by_symbol[t["symbol"]] = existing

            trades.append({
                "symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"],
                "price_usd": t["price"], "realized_pnl_usd": realized_pnl,
                "confidence_level": t["confidence_level"], "rationale": t["rationale"],
                "date": date,
            })

        equity = cash + sum(
            p["quantity"] * quotes[p["symbol"]]["price"]
            for p in positions_by_symbol.values() if p["quantity"] > 0 and p["symbol"] in quotes
        )
        open_positions_count = sum(1 for p in positions_by_symbol.values() if p["quantity"] > 0)
        snapshots.append({"equity_usd": equity, "cash_usd": cash,
                           "open_positions": open_positions_count, "date": date})

        recent = trades[-10:]
        recent_trades_summary = "\n".join(
            f"- {t['side']} {t['quantity']:.4f} {t['symbol']} @ ${t['price_usd']:.2f}"
            + (f" (realized P&L ${t['realized_pnl_usd']:+.2f})"
               if t["side"] == "sell" and t["realized_pnl_usd"] is not None else "")
            for t in recent
        ) or "(no trades yet)"

    return {"trades": trades, "snapshots": snapshots, "days_simulated": len(sorted_dates),
            "starting_cash_usd": starting_cash_usd}


def compute_backtest_stats(trades: list, snapshots: list) -> dict:
    """Extends trading_strategy_review.compute_stats() with what a
    backtest needs to judge net profitability, not just win rate:
    - profit_factor: gross wins / gross losses. The real tell for
      whether a high win rate is actually making money -- a strategy
      that wins often but small and loses rarely but big can have a
      great win rate and a profit_factor under 1.0 (net losing).
    - max_drawdown_pct: the worst peak-to-trough decline over the WHOLE
      run, not just where it happened to end up (compute_stats'
      current_drawdown_pct only reflects the final snapshot).
    - sample_size_ok: whether there are enough completed trades to
      trust these numbers at all -- see MIN_SELL_TRADES_FOR_SIGNAL.
    """
    base = compute_stats(trades, snapshots)

    sells = [t for t in trades if t["side"] == "sell"]
    gross_wins = sum(t["realized_pnl_usd"] for t in sells if (t.get("realized_pnl_usd") or 0) > 0)
    gross_losses = abs(sum(t["realized_pnl_usd"] for t in sells if (t.get("realized_pnl_usd") or 0) < 0))
    if gross_losses > 0:
        profit_factor = gross_wins / gross_losses
    elif gross_wins > 0:
        profit_factor = float("inf")  # profitable with zero losing trades
    else:
        profit_factor = None

    if snapshots:
        peak = snapshots[0]["equity_usd"]
        max_drawdown_pct = 0.0
        for s in snapshots:
            peak = max(peak, s["equity_usd"])
            if peak > 0:
                max_drawdown_pct = max(max_drawdown_pct, (peak - s["equity_usd"]) / peak)
    else:
        max_drawdown_pct = None

    return {
        **base,
        "net_pnl_usd": base["total_realized_pnl_usd"],
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_drawdown_pct,
        "sample_size_ok": base["sell_trades"] >= MIN_SELL_TRADES_FOR_SIGNAL,
    }
