"""
tasks/trading_strategy_review.py — the "self-improving" half of the
paper-trading vertical.

Per the project spec's Improvement System section (OBSERVE -> MEASURE ->
... -> DEPLOY -> MONITOR -> ROLLBACK) and its explicit warning against
uncontrolled self-modification: this task type never touches code. It
produces a new, versioned ROW of strategy *parameters* (the same schema
trading_cycle.py already enforces via tasks/trading_common.py), with a
human-readable rationale, that becomes the active version only after
passing the exact same validate_parameters() bounds check that would
apply to a hand-written config. Every prior version stays in the table —
this is deliberately a full history, not an overwrite, so the owner can
always see what changed, why, and roll back by re-activating an older
version.

The model is given REAL, code-computed statistics (win rate, realized
P&L, drawdown) — never asked to invent or recall them — and asked to
propose a hypothesis for the next version, explicitly framed as
untested. This is the OBSERVE/MEASURE/GENERATE-HYPOTHESIS part of the
loop; trading_cycle.py running under the new version over time is the
TEST/MONITOR part; the owner reviewing the version history
(GET /businesses/{id}/trading/strategy-versions) and this system's
existing drawdown circuit breaker are the ROLLBACK safeguards.
"""

import json

from tasks.trading_common import validate_parameters, StrategyParameterError

REQUIRED_RESULT_FIELDS = {"parameters", "rationale", "confidence_level"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}

SYSTEM_PROMPT = """You are reviewing the performance of a paper-trading (simulation only, no \
real money) strategy and proposing an updated set of strategy parameters. You will be given the \
strategy's current parameters and REAL, already-computed performance statistics — do not \
recompute or second-guess these numbers, and never invent statistics that weren't given to you.

Rules:
- This is a hypothesis for the NEXT period, not a claim that it will improve results. Never \
claim a parameter change is guaranteed to increase returns or reduce risk.
- Stay conservative: prefer small, explainable adjustments over large swings, especially with a \
small trade-history sample. A missing or thin track record should itself lead to more caution \
(e.g. lower max_position_pct or max_trade_pct_of_cash), not less.
- You may adjust the watchlist, but only to well-known, liquid, real ticker symbols — never \
invent a symbol.
- drawdown_halt_pct is a real circuit breaker that pauses trading entirely when tripped — do not \
propose raising it as a way to "avoid" a halt; that defeats its purpose. Only lower it, or leave \
it unchanged, unless you have a specific, stated reason tied to the actual statistics given.
- confidence_level (top-level, about this proposal itself) must be "low", "medium", or "high".
- rationale must explain, referencing the actual statistics given, what specifically changed \
and why.

Respond with ONLY a single JSON object (no markdown fences, no prose before or after): \
{"parameters": {"watchlist": [...], "max_position_pct": 0.0, "max_trade_pct_of_cash": 0.0, \
"max_open_positions": 0, "drawdown_halt_pct": 0.0, "min_confidence_to_trade": "low"|"medium"|"high"}, \
"rationale": "...", "confidence_level": "low"|"medium"|"high"}"""


class TradingStrategyReviewError(Exception):
    pass


def compute_stats(trades: list, snapshots: list) -> dict:
    """Pure arithmetic over already-fetched DB rows (dicts) — no DB
    access here, so this is directly unit-testable with hand-built
    fixtures. `trades` are paper_trades rows (newest-first or any order),
    `snapshots` are trading_snapshots rows ordered oldest-first."""
    sells = [t for t in trades if t["side"] == "sell"]
    total_realized_pnl = sum(t.get("realized_pnl_usd") or 0.0 for t in sells)
    wins = sum(1 for t in sells if (t.get("realized_pnl_usd") or 0.0) > 0)
    losses = sum(1 for t in sells if (t.get("realized_pnl_usd") or 0.0) < 0)
    win_rate = (wins / len(sells)) if sells else None

    if snapshots:
        equities = [s["equity_usd"] for s in snapshots]
        current_equity = equities[-1]
        peak_equity = max(equities)
        drawdown_pct = ((peak_equity - current_equity) / peak_equity) if peak_equity > 0 else 0.0
    else:
        current_equity = None
        peak_equity = None
        drawdown_pct = None

    return {
        "total_trades": len(trades),
        "sell_trades": len(sells),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_realized_pnl_usd": total_realized_pnl,
        "current_equity_usd": current_equity,
        "peak_equity_usd": peak_equity,
        "current_drawdown_pct": drawdown_pct,
    }


def _format_context(current_params, stats, recent_trades_summary):
    def fmt(v, suffix=""):
        return "n/a" if v is None else f"{v:.4f}{suffix}"

    return (
        f"Current strategy parameters:\n{json.dumps(current_params, indent=2)}\n\n"
        f"Real performance statistics (computed in code, not by you):\n"
        f"- total trades: {stats['total_trades']}\n"
        f"- completed (sell) trades: {stats['sell_trades']}\n"
        f"- wins: {stats['wins']}, losses: {stats['losses']}\n"
        f"- win rate: {fmt(stats['win_rate'])}\n"
        f"- total realized P&L: ${fmt(stats['total_realized_pnl_usd'])}\n"
        f"- current equity: ${fmt(stats['current_equity_usd'])}\n"
        f"- peak equity: ${fmt(stats['peak_equity_usd'])}\n"
        f"- current drawdown from peak: {fmt(stats['current_drawdown_pct'], ' (fraction, e.g. 0.05 = 5%)')}\n\n"
        f"Recent trade history:\n{recent_trades_summary or '(no trades yet)'}"
    )


def propose_strategy_update(current_params, stats, recent_trades_summary, client) -> dict:
    """One real LLM call. Returns {"parameters": <validated dict>,
    "rationale": str, "confidence_level": str}. Raises
    TradingStrategyReviewError on any structurally invalid response OR
    if the proposed parameters fail tasks.trading_common.validate_parameters
    — an out-of-bounds proposal is a failed task, never a silently-
    clamped one, so a bad review can never sneak a looser risk limit into
    production."""
    user_content = _format_context(current_params, stats, recent_trades_summary)
    raw_response = client.complete(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=900,
    )

    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise TradingStrategyReviewError(
            f"model did not return valid JSON: {e}. Raw response: {raw_response[:300]}"
        ) from e

    missing = REQUIRED_RESULT_FIELDS - set(parsed.keys())
    if missing:
        raise TradingStrategyReviewError(f"model's JSON missing required fields: {sorted(missing)}")
    if parsed["confidence_level"] not in CONFIDENCE_LEVELS:
        raise TradingStrategyReviewError(
            f"confidence_level must be low/medium/high, got: {parsed['confidence_level']!r}"
        )

    try:
        validated_params = validate_parameters(parsed["parameters"])
    except StrategyParameterError as e:
        raise TradingStrategyReviewError(f"proposed parameters rejected: {e}") from e

    return {
        "parameters": validated_params,
        "rationale": str(parsed["rationale"]),
        "confidence_level": parsed["confidence_level"],
    }
