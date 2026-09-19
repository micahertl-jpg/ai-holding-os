"""
tasks/trading_common.py — shared strategy-parameter schema and
validation for the paper-trading vertical (tasks/trading_cycle.py,
tasks/trading_strategy_review.py).

Kept in its own module (rather than duplicated, or living in one of the
two task files) because BOTH task types read/write this exact schema:
trading_cycle.py enforces these limits against the model's proposed
trades every cycle, and trading_strategy_review.py proposes NEW values
for these same fields as its self-improvement output. A single shared
`validate_parameters` is what keeps a strategy version that trading_cycle
would refuse to run from ever being saved as active in the first place.

Every numeric bound here is a HARD ceiling enforced in code — the model
(in either task type) can only ever propose a value; it can never bypass
these bounds, and a proposal outside them is rejected outright rather
than silently clamped, so the strategy_review task fails loudly instead
of quietly saving something looser than intended.
"""

REQUIRED_PARAM_FIELDS = {
    "watchlist",
    "max_position_pct",
    "max_trade_pct_of_cash",
    "max_open_positions",
    "drawdown_halt_pct",
    "min_confidence_to_trade",
}

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

# Absolute ceilings no strategy version — including one a future review
# proposes — may exceed, regardless of what the model suggests. These
# exist so a single bad/adversarial model response can't turn into an
# unbounded paper position; they are intentionally conservative for an
# MVP and are a separate, harder limit than the per-version parameters
# themselves.
ABSOLUTE_MAX_POSITION_PCT = 0.5
ABSOLUTE_MAX_TRADE_PCT_OF_CASH = 0.5
ABSOLUTE_MAX_OPEN_POSITIONS = 15
ABSOLUTE_MIN_DRAWDOWN_HALT_PCT = 0.05  # never allow disabling the circuit breaker

DEFAULT_STRATEGY_PARAMS = {
    "watchlist": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"],
    # Max value of any single position, as a fraction of total equity.
    "max_position_pct": 0.20,
    # Max size of any single trade, as a fraction of currently available cash.
    "max_trade_pct_of_cash": 0.10,
    "max_open_positions": 5,
    # Pause the trading agent (owner review required to resume) if equity
    # drawdown from its peak reaches this fraction. Real risk-limit
    # machinery, exercised now on paper so it is proven correct before
    # any future real-money vertical could ever depend on it.
    "drawdown_halt_pct": 0.15,
    # A proposed trade below this confidence is logged as skipped, never
    # executed — "low" confidence is observation-only in this MVP.
    "min_confidence_to_trade": "medium",
}


class StrategyParameterError(Exception):
    pass


def validate_parameters(params: dict) -> dict:
    """Raises StrategyParameterError with a specific reason on any
    invalid/out-of-bounds field. Returns the same dict on success (for
    call-site chaining), never modifies it — this is a hard gate, not a
    clamp; a strategy_review proposal that fails this is a failed task,
    not a silently-adjusted one."""
    if not isinstance(params, dict):
        raise StrategyParameterError(f"parameters must be a JSON object, got {type(params)}")

    missing = REQUIRED_PARAM_FIELDS - set(params.keys())
    if missing:
        raise StrategyParameterError(f"parameters missing required fields: {sorted(missing)}")

    watchlist = params["watchlist"]
    if not isinstance(watchlist, list) or not (1 <= len(watchlist) <= 20):
        raise StrategyParameterError("watchlist must be a list of 1-20 ticker symbols")
    for sym in watchlist:
        if not isinstance(sym, str) or not sym.isalnum() or not (1 <= len(sym) <= 8):
            raise StrategyParameterError(f"invalid ticker symbol in watchlist: {sym!r}")

    def _pct(name, ceiling):
        v = params[name]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise StrategyParameterError(f"{name} must be a number")
        if not (0 < v <= ceiling):
            raise StrategyParameterError(f"{name} must be in (0, {ceiling}], got {v}")
        return float(v)

    max_position_pct = _pct("max_position_pct", ABSOLUTE_MAX_POSITION_PCT)
    max_trade_pct_of_cash = _pct("max_trade_pct_of_cash", ABSOLUTE_MAX_TRADE_PCT_OF_CASH)

    max_open_positions = params["max_open_positions"]
    if (not isinstance(max_open_positions, int) or isinstance(max_open_positions, bool)
            or not (1 <= max_open_positions <= ABSOLUTE_MAX_OPEN_POSITIONS)):
        raise StrategyParameterError(
            f"max_open_positions must be an integer in [1, {ABSOLUTE_MAX_OPEN_POSITIONS}]"
        )

    drawdown_halt_pct = params["drawdown_halt_pct"]
    if (not isinstance(drawdown_halt_pct, (int, float)) or isinstance(drawdown_halt_pct, bool)
            or not (ABSOLUTE_MIN_DRAWDOWN_HALT_PCT <= drawdown_halt_pct <= 1.0)):
        raise StrategyParameterError(
            f"drawdown_halt_pct must be in [{ABSOLUTE_MIN_DRAWDOWN_HALT_PCT}, 1.0]"
        )

    min_confidence = params["min_confidence_to_trade"]
    if min_confidence not in CONFIDENCE_ORDER:
        raise StrategyParameterError(
            f"min_confidence_to_trade must be one of {sorted(CONFIDENCE_ORDER)}, got {min_confidence!r}"
        )

    return {
        "watchlist": [s.upper() for s in watchlist],
        "max_position_pct": max_position_pct,
        "max_trade_pct_of_cash": max_trade_pct_of_cash,
        "max_open_positions": max_open_positions,
        "drawdown_halt_pct": float(drawdown_halt_pct),
        "min_confidence_to_trade": min_confidence,
    }
