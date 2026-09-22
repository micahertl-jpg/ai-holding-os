"""
tasks/trading_cycle.py — the Automated Stock Trading vertical's core,
recurring task type. PAPER TRADING ONLY: nothing in this module, or
anywhere it's wired to (executor.py, api.py), can place a real order.
There is no brokerage integration in this codebase at all — that is a
structural fact, not a configuration flag someone could accidentally
enable. Per the project spec's Trading Safety section, real trading stays
disabled until explicitly, separately authorized and built.

The critical safety property of this file: the model PROPOSES a trade,
code DISPOSES. `decide_trades` gets the model's raw opinion (with a
mandatory confidence_level and rationale, framed as an estimate — see
SYSTEM_PROMPT). `apply_risk_limits` is a separate, pure, deterministic
function that enforces every position/trade/exposure/confidence limit in
`tasks/trading_common.py` regardless of what the model proposed. A model
that tried to propose an oversized or reckless trade would simply have it
clamped or skipped here — this is unit-tested directly (see
test_trading_cycle_offline.py) with no LLM involved, so the safety
guarantee doesn't depend on the model behaving.

Contract with the caller (executor.py's _handle_trading_cycle): `quotes`
passed to `apply_risk_limits`/returned by `run_trading_cycle` MUST include
a fresh quote for every symbol currently held (quantity > 0), not just
the current watchlist — otherwise equity/position-value math for a
symbol whose ticker rolled off the watchlist (via a strategy review)
would be silently wrong. `run_trading_cycle` enforces this itself by
fetching quotes for the union of the watchlist and current holdings, and
refuses to proceed if any held symbol's quote is missing.
"""

import json

from market_data import MarketDataError

REQUIRED_DECISION_FIELDS = {"symbol", "action", "confidence_level", "rationale"}
# size_pct is checked separately, below -- required for buy/sell, but
# allowed to be missing (defaulted to 0.0) for hold, where it's
# provably unused. See decide_trades()'s validation loop.
VALID_ACTIONS = {"buy", "sell", "hold"}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

# Below this, a trade is skipped as too small to bother with — avoids a
# strategy churning out $0.03 trades that mostly just generate noise in
# the ledger.
MIN_TRADE_USD = 1.0

SYSTEM_PROMPT = """You are a paper-trading equity analyst. This is a SIMULATION — no real \
money is ever moved, and you are told this explicitly so you never claim otherwise. You will \
be given the current paper portfolio (cash, open positions with unrealized P&L), recent trade \
history, and current prices for a watchlist of symbols. Decide buy/sell/hold for each symbol.

Rules:
- You are estimating and reasoning, not predicting. Never claim certainty about future price \
movement. Never claim a trade is guaranteed to be profitable or risk-free.
- confidence_level must be one of "low", "medium", "high", reflecting how much real signal (vs. \
speculation) supports this decision. Use "low" liberally — it is not penalized, and a dishonest \
"high" is worse than an honest "low".
- size_pct is a number from 0.0 to 1.0, and must ALWAYS be present in every decision object, with \
no exceptions: for "buy", the fraction of AVAILABLE CASH you'd want to deploy into this symbol; \
for "sell", the fraction of the CURRENT POSITION you'd want to close; for "hold", it has no \
effect on anything — always set it to 0.0 rather than leaving it out. Your suggestion is \
advisory — the system enforces its own hard position/trade-size/exposure limits regardless of \
what you propose here, so do not assume your suggested size will be used exactly as given.
- rationale must be a concrete, specific reason (1-2 sentences) — never a generic statement that \
could apply to any symbol.
- Only decide on symbols you were actually given a current price for.

Respond with ONLY a single JSON object (no markdown fences, no prose before or after): \
{"decisions": [{"symbol": "...", "action": "buy"|"sell"|"hold", "confidence_level": "low"|"medium"|"high", \
"size_pct": 0.0, "rationale": "..."}, ...]} — one entry per symbol you were given a price for."""


class TradingCycleError(Exception):
    pass


def _format_context(cash_usd, positions, quotes, strategy_params, recent_trades_summary):
    position_lines = []
    for p in positions:
        if p["quantity"] <= 0:
            continue
        quote = quotes.get(p["symbol"])
        mark = quote["price"] if quote else None
        unrealized = (mark - p["avg_cost_usd"]) * p["quantity"] if mark is not None else None
        position_lines.append(
            f"- {p['symbol']}: {p['quantity']:.4f} shares @ avg cost ${p['avg_cost_usd']:.2f}"
            + (f", current price ${mark:.2f}, unrealized P&L ${unrealized:+.2f}" if mark is not None else "")
        )
    positions_block = "\n".join(position_lines) if position_lines else "(no open positions)"

    quote_lines = [f"- {sym}: ${q['price']:.2f} (as of {q['as_of']})" for sym, q in sorted(quotes.items())]
    quotes_block = "\n".join(quote_lines)

    return (
        f"Available cash: ${cash_usd:.2f}\n\n"
        f"Open positions:\n{positions_block}\n\n"
        f"Current prices:\n{quotes_block}\n\n"
        f"Strategy limits in force (informational — the system enforces these itself): "
        f"max {strategy_params['max_position_pct']*100:.0f}% of equity in one symbol, "
        f"max {strategy_params['max_trade_pct_of_cash']*100:.0f}% of cash per trade, "
        f"max {strategy_params['max_open_positions']} open positions, "
        f"minimum confidence to act on: {strategy_params['min_confidence_to_trade']}.\n\n"
        f"Recent trade history:\n{recent_trades_summary or '(no trades yet)'}"
    )


def decide_trades(cash_usd, positions, strategy_params, recent_trades_summary, quotes, client) -> list:
    """One real LLM call asking for a decision on every symbol in
    `quotes`. Returns a list of validated raw decision dicts. Raises
    TradingCycleError on any structurally invalid model response — never
    fabricates or drops a bad decision silently."""
    if not quotes:
        raise TradingCycleError("no quotes available — nothing to decide on")

    user_content = _format_context(cash_usd, positions, quotes, strategy_params, recent_trades_summary)
    raw_response = client.complete(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=1200,
    )

    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise TradingCycleError(
            f"model did not return valid JSON: {e}. Raw response: {raw_response[:300]}"
        ) from e

    decisions = parsed.get("decisions")
    if not isinstance(decisions, list):
        raise TradingCycleError(f"model's JSON missing a 'decisions' list. Got: {parsed}")

    validated = []
    for i, d in enumerate(decisions):
        if not isinstance(d, dict):
            raise TradingCycleError(f"decision[{i}] is not an object: {d!r}")
        missing = REQUIRED_DECISION_FIELDS - set(d.keys())
        if missing:
            raise TradingCycleError(f"decision[{i}] missing fields {sorted(missing)}: {d}")
        if d["symbol"] not in quotes:
            raise TradingCycleError(
                f"decision[{i}] is for symbol {d['symbol']!r}, which wasn't in the quotes given"
            )
        if d["action"] not in VALID_ACTIONS:
            raise TradingCycleError(f"decision[{i}] has invalid action {d['action']!r}")
        if d["confidence_level"] not in CONFIDENCE_ORDER:
            raise TradingCycleError(f"decision[{i}] has invalid confidence_level {d['confidence_level']!r}")

        if "size_pct" not in d:
            # The prompt says size_pct "has no effect" for hold, and a
            # real model has been seen (live) reading that as "may be
            # omitted" rather than "always include it, just use 0.0" --
            # safe to default ONLY for hold, since apply_risk_limits()
            # never reads size_pct on its hold branch at all. A missing
            # size_pct on a buy/sell is a genuine, unresolvable
            # ambiguity (silently guessing a size would be exactly the
            # kind of fabrication this codebase never does), so that
            # still fails loudly below.
            if d["action"] != "hold":
                raise TradingCycleError(
                    f"decision[{i}] missing required field 'size_pct' (only ever optional for "
                    f"action='hold', where it has no effect): {d}"
                )
            size_pct = 0.0
        else:
            try:
                size_pct = float(d["size_pct"])
            except (TypeError, ValueError):
                raise TradingCycleError(f"decision[{i}] has non-numeric size_pct: {d['size_pct']!r}")

        validated.append({
            "symbol": d["symbol"],
            "action": d["action"],
            "confidence_level": d["confidence_level"],
            "size_pct": max(0.0, min(1.0, size_pct)),
            "rationale": str(d["rationale"]),
        })
    return validated


def apply_risk_limits(decisions, cash_usd, positions, quotes, strategy_params) -> list:
    """Pure, deterministic, no LLM/network involved — this is the
    function that actually enforces safety, independent of model
    behavior. Every decision comes back as exactly one record: either
    executed=True with a clamped quantity/price, or executed=False with a
    skip_reason explaining exactly why. Nothing is ever silently dropped.
    """
    positions_by_symbol = {p["symbol"]: p for p in positions}
    watchlist = set(strategy_params["watchlist"])
    max_position_pct = strategy_params["max_position_pct"]
    max_trade_pct_of_cash = strategy_params["max_trade_pct_of_cash"]
    max_open_positions = strategy_params["max_open_positions"]
    min_confidence = CONFIDENCE_ORDER[strategy_params["min_confidence_to_trade"]]

    equity = cash_usd + sum(
        p["quantity"] * quotes[p["symbol"]]["price"]
        for p in positions if p["quantity"] > 0 and p["symbol"] in quotes
    )
    open_count = sum(1 for p in positions if p["quantity"] > 0)
    remaining_cash = cash_usd  # mutated as buys are processed, so a run of buys can't overspend

    results = []
    for d in decisions:
        symbol = d["symbol"]
        price = quotes[symbol]["price"]
        base = {"symbol": symbol, "action": d["action"],
                "confidence_level": d["confidence_level"], "rationale": d["rationale"]}

        if d["action"] == "hold":
            results.append({**base, "executed": False, "skip_reason": "model chose hold"})
            continue

        if CONFIDENCE_ORDER[d["confidence_level"]] < min_confidence:
            results.append({**base, "executed": False,
                             "skip_reason": f"confidence {d['confidence_level']} below "
                                            f"strategy minimum {strategy_params['min_confidence_to_trade']}"})
            continue

        if d["action"] == "buy":
            if symbol not in watchlist:
                results.append({**base, "executed": False,
                                 "skip_reason": "symbol is not in the strategy's watchlist — "
                                                "buys are restricted to the configured universe"})
                continue

            existing = positions_by_symbol.get(symbol, {"quantity": 0})
            is_new_position = existing["quantity"] <= 0
            if is_new_position and open_count >= max_open_positions:
                results.append({**base, "executed": False,
                                 "skip_reason": f"max_open_positions ({max_open_positions}) reached"})
                continue

            proposed_dollars = d["size_pct"] * remaining_cash
            proposed_dollars = min(proposed_dollars, max_trade_pct_of_cash * cash_usd)
            current_position_value = existing["quantity"] * price
            position_headroom = max(0.0, max_position_pct * equity - current_position_value)
            proposed_dollars = min(proposed_dollars, position_headroom, remaining_cash)

            if proposed_dollars < MIN_TRADE_USD:
                results.append({**base, "executed": False,
                                 "skip_reason": f"clamped trade size (${proposed_dollars:.2f}) is "
                                                f"below the ${MIN_TRADE_USD:.2f} minimum"})
                continue

            quantity = proposed_dollars / price
            remaining_cash -= proposed_dollars
            if is_new_position:
                open_count += 1
            positions_by_symbol[symbol] = {"quantity": existing["quantity"] + quantity,
                                            "avg_cost_usd": existing.get("avg_cost_usd", 0)}
            results.append({**base, "executed": True, "side": "buy",
                             "quantity": quantity, "price": price})

        elif d["action"] == "sell":
            existing = positions_by_symbol.get(symbol, {"quantity": 0})
            if existing["quantity"] <= 0:
                results.append({**base, "executed": False,
                                 "skip_reason": "no open position to sell"})
                continue

            proposed_qty = min(d["size_pct"] * existing["quantity"], existing["quantity"])
            if proposed_qty * price < MIN_TRADE_USD:
                results.append({**base, "executed": False,
                                 "skip_reason": f"clamped trade size (${proposed_qty * price:.2f}) is "
                                                f"below the ${MIN_TRADE_USD:.2f} minimum"})
                continue

            new_qty = existing["quantity"] - proposed_qty
            if new_qty <= 1e-9:
                open_count -= 1
                new_qty = 0.0
            positions_by_symbol[symbol] = {"quantity": new_qty,
                                            "avg_cost_usd": existing.get("avg_cost_usd", 0)}
            results.append({**base, "executed": True, "side": "sell",
                             "quantity": proposed_qty, "price": price})

    return results


def run_trading_cycle(cash_usd, positions, strategy_params, recent_trades_summary,
                       market_client, llm_client) -> dict:
    """Orchestrates one full cycle: fetch quotes (watchlist + every
    currently-held symbol, so equity math is never based on a stale
    price for a position that rolled off the watchlist), ask the model
    to decide, then enforce risk limits in code. Returns
    {"quotes", "quote_errors", "raw_decisions", "trades"}.

    Raises TradingCycleError (never silently proceeds) if: a currently-
    held symbol's quote couldn't be fetched (equity would be unknown), or
    any fetched quote is mock data (ALPHAVANTAGE_API_KEY not configured —
    this system never paper-trades on fabricated prices outside of an
    explicit offline test)."""
    symbols_to_quote = set(strategy_params["watchlist"]) | {
        p["symbol"] for p in positions if p["quantity"] > 0
    }
    quotes, quote_errors = {}, {}
    for symbol in sorted(symbols_to_quote):
        try:
            quotes[symbol] = market_client.get_quote(symbol)
        except MarketDataError as e:
            quote_errors[symbol] = str(e)

    held_missing = sorted(
        p["symbol"] for p in positions if p["quantity"] > 0 and p["symbol"] not in quotes
    )
    if held_missing:
        # The real reason each quote fetch failed (e.g. Alpha Vantage's
        # exact rate-limit message) was already captured in
        # quote_errors above -- surfacing it here, instead of just
        # naming the symbol, is the difference between the owner
        # immediately seeing "rate limited, try again later" and
        # having to go dig through logs (or ask) to find out why a
        # symbol they've traded before suddenly has no quote.
        reasons = "; ".join(f"{sym}: {quote_errors.get(sym, 'no error captured')}"
                             for sym in held_missing)
        raise TradingCycleError(
            f"missing live quotes for currently-held symbols {held_missing}; "
            f"refusing to trade without knowing their current value ({reasons})"
        )

    mocked = sorted(sym for sym, q in quotes.items() if q.get("mock"))
    if mocked:
        raise TradingCycleError(
            f"refusing to trade — mock market data for {mocked} "
            f"(ALPHAVANTAGE_API_KEY not configured; see README ACTION REQUIRED)"
        )

    if not quotes:
        raise TradingCycleError("no live quotes available for any watchlist/held symbol this cycle")

    raw_decisions = decide_trades(cash_usd, positions, strategy_params,
                                   recent_trades_summary, quotes, llm_client)
    trades = apply_risk_limits(raw_decisions, cash_usd, positions, quotes, strategy_params)

    return {"quotes": quotes, "quote_errors": quote_errors,
            "raw_decisions": raw_decisions, "trades": trades}
