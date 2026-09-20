"""
tasks/live_trading_safety.py — a SECOND, independent safety layer for
real-money trading, applied on top of (never instead of) the existing
percentage-based limits in tasks/trading_cycle.py's apply_risk_limits().

Why a separate layer rather than just raising the existing percentage
ceilings: a percentage of equity means nothing in absolute risk terms
until you know the account size, and for a small live account (a few
hundred to a couple thousand real dollars) a percentage-only limit can
still be large enough in absolute terms to hurt. This module adds hard,
env-configurable dollar ceilings that apply regardless of what
percentage-based math already allowed, plus a kill switch that can halt
all live trading instantly via a single env var -- no dashboard access,
no code deploy, no waiting for the next scheduled pause check.

Same split as everywhere else trading-related in this codebase: this
file is pure, deterministic, and has zero LLM/network involvement, so
its behavior is fully guaranteed by its own unit tests
(test_live_trading_safety_offline.py), never by trusting the model or
the broker to behave.
"""

import os

# Absolute per-trade ceiling in real USD. A trade whose clamped size
# (from apply_risk_limits, already percentage-limited) still exceeds
# this is clamped down further, never allowed through at full size.
# Default sized for a $100 starting live account: the existing 10%-of-
# cash percentage limit already caps a single trade at ~$10 there, so
# this mostly sits as a backstop behind it -- it stays meaningful (and
# doesn't silently balloon) even after the account grows or the
# percentage limits get loosened, without needing to remember to raise
# a second number in lockstep. Raise deliberately as real capital added.
LIVE_MAX_TRADE_USD = float(os.environ.get("LIVE_TRADING_MAX_TRADE_USD", "15.0"))

# Absolute real-dollar loss ceiling for one calendar day of live
# trading. Once today's realized P&L (sells only -- unrealized
# mark-to-market swings on open positions don't count against this,
# same as how realized_pnl_usd is only ever set on 'sell' rows
# elsewhere in this codebase) breaches this, every remaining trade this
# cycle (and, via the executor halting the agent, every future cycle
# until owner review) is blocked. Default is a 7% circuit breaker on a
# $100 starting account -- tight enough to actually cap downside, loose
# enough not to trip from one ordinary losing trade.
LIVE_MAX_DAILY_LOSS_USD = float(os.environ.get("LIVE_TRADING_MAX_DAILY_LOSS_USD", "7.0"))

# Below this, a live trade is skipped as not worth the complexity/risk
# of a tiny real order rather than clamped down further -- same
# reasoning as trading_cycle.MIN_TRADE_USD, kept as its own constant
# here since live trading may want a different floor than paper. Kept
# low relative to LIVE_MAX_TRADE_USD so a $100-account trade clamped
# down to the per-trade cap still has room to actually execute instead
# of being skipped as too small.
MIN_LIVE_TRADE_USD = 2.0


def is_kill_switch_active() -> bool:
    """A single env var, checked fresh every call (never cached), that
    unconditionally blocks all live order placement when set -- the
    fastest possible way to halt live trading in an emergency: change
    one Railway env var, no code deploy, no dashboard click, takes
    effect on the very next cycle."""
    return os.environ.get("LIVE_TRADING_KILL_SWITCH", "").strip().lower() in ("1", "true", "yes", "on")


def apply_live_safety_caps(trades: list, todays_realized_pnl_usd: float,
                            max_trade_usd: float = None, max_daily_loss_usd: float = None) -> list:
    """Takes the trade list already produced by
    tasks/trading_cycle.py's apply_risk_limits() (percentage-limited)
    and applies the absolute-dollar caps above on top. Returns a new
    list in the same shape -- every trade comes back with either its
    original executed=True (unchanged or with quantity clamped down)
    or executed=False with skip_reason explaining exactly why, same
    "nothing silently dropped" contract as apply_risk_limits(). Pure,
    deterministic, no I/O.

    todays_realized_pnl_usd should be the sum of realized_pnl_usd for
    every live sell so far today (negative = net loss) -- computed by
    the caller from live_trades, not from anything this function
    fetches itself."""
    max_trade_usd = LIVE_MAX_TRADE_USD if max_trade_usd is None else max_trade_usd
    max_daily_loss_usd = LIVE_MAX_DAILY_LOSS_USD if max_daily_loss_usd is None else max_daily_loss_usd

    daily_loss_halted = todays_realized_pnl_usd <= -abs(max_daily_loss_usd)

    results = []
    for t in trades:
        if not t.get("executed"):
            results.append(t)
            continue

        if daily_loss_halted:
            results.append({**t, "executed": False,
                             "skip_reason": f"live daily loss halt: today's realized P&L "
                                            f"(${todays_realized_pnl_usd:.2f}) has reached the "
                                            f"${max_daily_loss_usd:.2f} limit -- no further live "
                                            f"trades until owner review"})
            continue

        trade_usd = t["quantity"] * t["price"]
        if trade_usd <= max_trade_usd:
            results.append(t)
            continue

        clamped_quantity = max_trade_usd / t["price"]
        clamped_usd = clamped_quantity * t["price"]
        if clamped_usd < MIN_LIVE_TRADE_USD:
            results.append({**t, "executed": False,
                             "skip_reason": f"clamped to the ${max_trade_usd:.2f} live per-trade cap "
                                            f"would leave a trade (${clamped_usd:.2f}) below the "
                                            f"${MIN_LIVE_TRADE_USD:.2f} minimum"})
            continue

        results.append({**t, "quantity": clamped_quantity,
                         "live_cap_applied": True,
                         "pre_cap_quantity": t["quantity"]})

    return results
