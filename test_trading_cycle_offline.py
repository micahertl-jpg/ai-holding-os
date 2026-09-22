"""
test_trading_cycle_offline.py — real tests for tasks/trading_cycle.py.

The core safety claim under test: apply_risk_limits() enforces every
limit in code, regardless of what a (possibly adversarial or simply
wrong) model proposes. Every test in the "risk limit" section below
constructs a raw decision that WOULD violate a limit if taken at face
value, and asserts the code clamps or skips it — no LLM involved in that
section at all, so the guarantee doesn't depend on model behavior.
"""

import json

from market_data import MarketDataError
from tasks.trading_common import DEFAULT_STRATEGY_PARAMS, validate_parameters, StrategyParameterError
from tasks.trading_cycle import (
    decide_trades, apply_risk_limits, run_trading_cycle, TradingCycleError,
)


class _FakeLLMClient:
    def __init__(self, canned_response):
        self.canned_response = canned_response
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def complete(self, messages, system=None, max_tokens=1000):
        return self.canned_response


class _FakeMarketClient:
    def __init__(self, prices, mock=False, raise_for=()):
        self.prices = prices
        self.mock = mock
        self.raise_for = set(raise_for)

    def get_quote(self, symbol):
        if symbol in self.raise_for:
            raise MarketDataError(f"no data for {symbol}")
        return {"symbol": symbol, "price": self.prices[symbol], "as_of": "2026-01-01",
                "mock": self.mock}


PARAMS = validate_parameters(dict(DEFAULT_STRATEGY_PARAMS))  # watchlist: AAPL MSFT GOOGL AMZN NVDA


# ---------------------------------------------------------------------
# tasks/trading_common.py — parameter validation
# ---------------------------------------------------------------------

def test_validate_parameters_accepts_the_default():
    validated = validate_parameters(dict(DEFAULT_STRATEGY_PARAMS))
    assert validated["watchlist"] == ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]
    print("PASS: validate_parameters accepts the shipped default parameters")


def test_validate_parameters_rejects_missing_field():
    bad = {k: v for k, v in DEFAULT_STRATEGY_PARAMS.items() if k != "drawdown_halt_pct"}
    try:
        validate_parameters(bad)
        assert False, "expected StrategyParameterError"
    except StrategyParameterError as e:
        assert "drawdown_halt_pct" in str(e)
    print("PASS: validate_parameters rejects a proposal missing a required field")


def test_validate_parameters_rejects_over_absolute_ceiling():
    bad = dict(DEFAULT_STRATEGY_PARAMS)
    bad["max_position_pct"] = 0.9  # above ABSOLUTE_MAX_POSITION_PCT (0.5)
    try:
        validate_parameters(bad)
        assert False, "expected StrategyParameterError"
    except StrategyParameterError:
        pass
    print("PASS: validate_parameters rejects a max_position_pct above the absolute ceiling "
          "-- a strategy review can never propose past this, whatever it argues")


def test_validate_parameters_rejects_disabling_the_circuit_breaker():
    bad = dict(DEFAULT_STRATEGY_PARAMS)
    bad["drawdown_halt_pct"] = 0.0
    try:
        validate_parameters(bad)
        assert False, "expected StrategyParameterError"
    except StrategyParameterError:
        pass
    print("PASS: validate_parameters refuses a drawdown_halt_pct of 0 -- the circuit "
          "breaker can never be fully disabled by a proposal")


def test_validate_parameters_rejects_invalid_confidence_floor():
    bad = dict(DEFAULT_STRATEGY_PARAMS)
    bad["min_confidence_to_trade"] = "extreme"
    try:
        validate_parameters(bad)
        assert False, "expected StrategyParameterError"
    except StrategyParameterError:
        pass
    print("PASS: validate_parameters rejects an invalid min_confidence_to_trade value")


# ---------------------------------------------------------------------
# decide_trades — model output validation
# ---------------------------------------------------------------------

def test_decide_trades_parses_a_valid_response():
    quotes = {"AAPL": {"price": 200.0, "as_of": "2026-01-01", "mock": False}}
    canned = json.dumps({"decisions": [
        {"symbol": "AAPL", "action": "buy", "confidence_level": "high",
         "size_pct": 0.5, "rationale": "Strong recent momentum."},
    ]})
    decisions = decide_trades(10000, [], PARAMS, "", quotes, _FakeLLMClient(canned))
    assert decisions == [{"symbol": "AAPL", "action": "buy", "confidence_level": "high",
                           "size_pct": 0.5, "rationale": "Strong recent momentum."}]
    print("PASS: decide_trades parses a valid model response into a validated decision")


def test_decide_trades_rejects_non_json():
    quotes = {"AAPL": {"price": 200.0, "as_of": "x", "mock": False}}
    try:
        decide_trades(10000, [], PARAMS, "", quotes, _FakeLLMClient("not json at all"))
        assert False, "expected TradingCycleError"
    except TradingCycleError as e:
        assert "valid JSON" in str(e)
    print("PASS: decide_trades fails loudly on non-JSON model output, never fabricates a decision")


def test_decide_trades_rejects_decision_for_unquoted_symbol():
    quotes = {"AAPL": {"price": 200.0, "as_of": "x", "mock": False}}
    canned = json.dumps({"decisions": [
        {"symbol": "TSLA", "action": "buy", "confidence_level": "high", "size_pct": 0.1,
         "rationale": "x"},
    ]})
    try:
        decide_trades(10000, [], PARAMS, "", quotes, _FakeLLMClient(canned))
        assert False, "expected TradingCycleError"
    except TradingCycleError as e:
        assert "TSLA" in str(e)
    print("PASS: decide_trades rejects a decision for a symbol it wasn't given a quote for")


def test_decide_trades_rejects_invalid_action():
    quotes = {"AAPL": {"price": 200.0, "as_of": "x", "mock": False}}
    canned = json.dumps({"decisions": [
        {"symbol": "AAPL", "action": "short", "confidence_level": "high", "size_pct": 0.1,
         "rationale": "x"},
    ]})
    try:
        decide_trades(10000, [], PARAMS, "", quotes, _FakeLLMClient(canned))
        assert False, "expected TradingCycleError"
    except TradingCycleError:
        pass
    print("PASS: decide_trades rejects an action outside buy/sell/hold")


def test_decide_trades_defaults_a_missing_size_pct_to_zero_for_hold():
    # A real bug found live: the system prompt says size_pct "has no
    # effect" for hold, and a real model read that as "may be omitted"
    # rather than "always include it, just use 0.0" -- this must not
    # fail the whole cycle over a field that apply_risk_limits() never
    # even reads on its hold branch.
    quotes = {"AAPL": {"price": 200.0, "as_of": "x", "mock": False}}
    canned = json.dumps({"decisions": [
        {"symbol": "AAPL", "action": "hold", "confidence_level": "medium", "rationale": "x"},
    ]})
    decisions = decide_trades(10000, [], PARAMS, "", quotes, _FakeLLMClient(canned))
    assert decisions == [{"symbol": "AAPL", "action": "hold", "confidence_level": "medium",
                           "size_pct": 0.0, "rationale": "x"}]
    print("PASS: decide_trades defaults a missing size_pct to 0.0 for a hold decision, "
          "rather than failing the whole cycle over a field hold never uses")


def test_decide_trades_still_rejects_a_missing_size_pct_for_buy_or_sell():
    # The default above must NEVER extend to buy/sell -- a missing size
    # there is a genuine, unresolvable ambiguity about how much real
    # cash/position to move, and silently guessing at it would be
    # exactly the kind of fabrication this codebase never does.
    quotes = {"AAPL": {"price": 200.0, "as_of": "x", "mock": False}}
    for action in ("buy", "sell"):
        canned = json.dumps({"decisions": [
            {"symbol": "AAPL", "action": action, "confidence_level": "high", "rationale": "x"},
        ]})
        try:
            decide_trades(10000, [], PARAMS, "", quotes, _FakeLLMClient(canned))
            assert False, f"expected TradingCycleError for a {action} missing size_pct"
        except TradingCycleError as e:
            assert "size_pct" in str(e)
    print("PASS: decide_trades still fails loudly on a missing size_pct for buy/sell -- the "
          "hold-only default never weakens this")


# ---------------------------------------------------------------------
# apply_risk_limits — the actual safety enforcement, no LLM involved
# ---------------------------------------------------------------------

def test_buy_is_clamped_to_max_trade_pct_of_cash():
    quotes = {"AAPL": {"price": 100.0, "as_of": "x", "mock": False}}
    # Model asks to deploy 100% of cash; strategy caps a single trade at
    # max_trade_pct_of_cash (10% by default).
    decisions = [{"symbol": "AAPL", "action": "buy", "confidence_level": "high",
                  "size_pct": 1.0, "rationale": "x"}]
    result = apply_risk_limits(decisions, 10000.0, [], quotes, PARAMS)
    assert len(result) == 1 and result[0]["executed"]
    spent = result[0]["quantity"] * result[0]["price"]
    assert abs(spent - 1000.0) < 0.01, f"expected clamp to 10% of $10000 = $1000, got ${spent}"
    print("PASS: a buy asking for 100% of cash is clamped to max_trade_pct_of_cash (10%), "
          "regardless of what the model proposed")


def test_buy_is_clamped_to_max_position_pct_of_equity():
    quotes = {"AAPL": {"price": 100.0, "as_of": "x", "mock": False}}
    # Already holding $1900 of AAPL out of $10000 equity (19%); strategy
    # caps a single symbol at 20% of equity -- so only ~$100 more room.
    positions = [{"symbol": "AAPL", "quantity": 19.0, "avg_cost_usd": 100.0}]
    cash = 8100.0  # equity = 8100 + 19*100 = 10000
    decisions = [{"symbol": "AAPL", "action": "buy", "confidence_level": "high",
                  "size_pct": 1.0, "rationale": "x"}]
    result = apply_risk_limits(decisions, cash, positions, quotes, PARAMS)
    assert result[0]["executed"], result
    spent = result[0]["quantity"] * result[0]["price"]
    assert spent <= 100.0 + 0.01, f"expected <= $100 headroom under max_position_pct, got ${spent}"
    print("PASS: a buy that would push a single symbol over max_position_pct of equity "
          "is clamped to the remaining headroom")


def test_buy_skipped_when_max_open_positions_reached():
    quotes = {"AAPL": {"price": 100.0, "as_of": "x", "mock": False}}
    # 5 other symbols already open (== default max_open_positions), and
    # this decision would open a 6th, brand-new position.
    positions = [{"symbol": s, "quantity": 1.0, "avg_cost_usd": 100.0}
                 for s in ["MSFT", "GOOGL", "AMZN", "NVDA", "META"]]
    decisions = [{"symbol": "AAPL", "action": "buy", "confidence_level": "high",
                  "size_pct": 0.05, "rationale": "x"}]
    result = apply_risk_limits(decisions, 10000.0, positions, quotes, PARAMS)
    assert result[0]["executed"] is False
    assert "max_open_positions" in result[0]["skip_reason"]
    print("PASS: a new-position buy is skipped once max_open_positions is already reached")


def test_buy_of_symbol_outside_watchlist_is_always_skipped():
    quotes = {"TSLA": {"price": 300.0, "as_of": "x", "mock": False}}
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL"]})
    decisions = [{"symbol": "TSLA", "action": "buy", "confidence_level": "high",
                  "size_pct": 0.05, "rationale": "x"}]
    result = apply_risk_limits(decisions, 10000.0, [], quotes, params)
    assert result[0]["executed"] is False
    assert "watchlist" in result[0]["skip_reason"]
    print("PASS: a buy for a symbol outside the strategy's watchlist is always skipped, "
          "even though the caller happened to supply a quote for it")


def test_sell_is_allowed_for_a_held_symbol_outside_the_current_watchlist():
    # A position left over from an older watchlist (e.g. after a strategy
    # review dropped it) must still be sellable.
    quotes = {"IBM": {"price": 150.0, "as_of": "x", "mock": False}}
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "watchlist": ["AAPL"]})
    positions = [{"symbol": "IBM", "quantity": 10.0, "avg_cost_usd": 100.0}]
    decisions = [{"symbol": "IBM", "action": "sell", "confidence_level": "high",
                  "size_pct": 1.0, "rationale": "x"}]
    result = apply_risk_limits(decisions, 0.0, positions, quotes, params)
    assert result[0]["executed"] is True and result[0]["side"] == "sell"
    print("PASS: selling a legacy position works even when its symbol has since rolled "
          "off the watchlist -- a strategy change can never strand a position with no exit")


def test_low_confidence_decision_is_skipped_when_below_strategy_floor():
    quotes = {"AAPL": {"price": 100.0, "as_of": "x", "mock": False}}
    decisions = [{"symbol": "AAPL", "action": "buy", "confidence_level": "low",
                  "size_pct": 0.5, "rationale": "x"}]  # default floor is "medium"
    result = apply_risk_limits(decisions, 10000.0, [], quotes, PARAMS)
    assert result[0]["executed"] is False
    assert "confidence" in result[0]["skip_reason"]
    print("PASS: a low-confidence decision never trades when the strategy's floor is medium")


def test_hold_never_trades():
    quotes = {"AAPL": {"price": 100.0, "as_of": "x", "mock": False}}
    decisions = [{"symbol": "AAPL", "action": "hold", "confidence_level": "high",
                  "size_pct": 0.5, "rationale": "x"}]
    result = apply_risk_limits(decisions, 10000.0, [], quotes, PARAMS)
    assert result[0]["executed"] is False
    print("PASS: hold decisions never execute a trade regardless of confidence/size_pct")


def test_sell_cannot_exceed_current_position():
    quotes = {"AAPL": {"price": 100.0, "as_of": "x", "mock": False}}
    positions = [{"symbol": "AAPL", "quantity": 5.0, "avg_cost_usd": 90.0}]
    decisions = [{"symbol": "AAPL", "action": "sell", "confidence_level": "high",
                  "size_pct": 1.0, "rationale": "x"}]
    result = apply_risk_limits(decisions, 0.0, positions, quotes, PARAMS)
    assert result[0]["quantity"] <= 5.0
    print("PASS: a sell can never liquidate more than the currently-held quantity")


def test_tiny_trade_is_skipped_as_below_minimum():
    quotes = {"AAPL": {"price": 100.0, "as_of": "x", "mock": False}}
    decisions = [{"symbol": "AAPL", "action": "buy", "confidence_level": "high",
                  "size_pct": 0.00001, "rationale": "x"}]
    result = apply_risk_limits(decisions, 10000.0, [], quotes, PARAMS)
    assert result[0]["executed"] is False
    assert "minimum" in result[0]["skip_reason"]
    print("PASS: a trade clamped below the $1 minimum is skipped, not executed as a "
          "near-zero-size trade")


# ---------------------------------------------------------------------
# run_trading_cycle — the orchestration layer's own refusal conditions
# ---------------------------------------------------------------------

def test_run_trading_cycle_refuses_mock_market_data():
    market_client = _FakeMarketClient({s: 100.0 for s in PARAMS["watchlist"]}, mock=True)
    try:
        run_trading_cycle(10000.0, [], PARAMS, "", market_client, _FakeLLMClient('{"decisions": []}'))
        assert False, "expected TradingCycleError"
    except TradingCycleError as e:
        assert "mock market data" in str(e)
    print("PASS: run_trading_cycle refuses to trade when quotes are mock data "
          "(no ALPHAVANTAGE_API_KEY configured)")


def test_run_trading_cycle_refuses_when_a_held_symbols_quote_is_missing():
    positions = [{"symbol": "IBM", "quantity": 10.0, "avg_cost_usd": 100.0}]
    market_client = _FakeMarketClient({s: 100.0 for s in PARAMS["watchlist"]}, raise_for=["IBM"])
    try:
        run_trading_cycle(10000.0, positions, PARAMS, "", market_client,
                           _FakeLLMClient('{"decisions": []}'))
        assert False, "expected TradingCycleError"
    except TradingCycleError as e:
        assert "IBM" in str(e)
        # The real underlying reason (e.g. Alpha Vantage's exact
        # rate-limit text) must be in the message too, not just the
        # symbol name -- otherwise the owner sees "missing quotes" with
        # no way to tell a rate limit apart from a delisted symbol or a
        # network failure without digging through logs.
        assert "no data for IBM" in str(e)
    print("PASS: run_trading_cycle refuses to proceed if it can't get a fresh quote for a "
          "currently-held symbol -- equity would otherwise be silently wrong -- and the error "
          "includes the real underlying reason, not just the symbol name")


def test_run_trading_cycle_succeeds_end_to_end_with_real_shaped_inputs():
    prices = {s: 100.0 for s in PARAMS["watchlist"]}
    market_client = _FakeMarketClient(prices)
    canned = json.dumps({"decisions": [
        {"symbol": s, "action": "hold", "confidence_level": "low", "size_pct": 0.0, "rationale": "x"}
        for s in PARAMS["watchlist"]
    ]})
    result = run_trading_cycle(10000.0, [], PARAMS, "", market_client, _FakeLLMClient(canned))
    assert result["quotes"] and not result["quote_errors"]
    assert all(t["executed"] is False for t in result["trades"])
    print("PASS: run_trading_cycle runs end-to-end with real-shaped inputs and a hold-only "
          "response, executing nothing")


if __name__ == "__main__":
    test_validate_parameters_accepts_the_default()
    test_validate_parameters_rejects_missing_field()
    test_validate_parameters_rejects_over_absolute_ceiling()
    test_validate_parameters_rejects_disabling_the_circuit_breaker()
    test_validate_parameters_rejects_invalid_confidence_floor()
    test_decide_trades_parses_a_valid_response()
    test_decide_trades_rejects_non_json()
    test_decide_trades_rejects_decision_for_unquoted_symbol()
    test_decide_trades_rejects_invalid_action()
    test_decide_trades_defaults_a_missing_size_pct_to_zero_for_hold()
    test_decide_trades_still_rejects_a_missing_size_pct_for_buy_or_sell()
    test_buy_is_clamped_to_max_trade_pct_of_cash()
    test_buy_is_clamped_to_max_position_pct_of_equity()
    test_buy_skipped_when_max_open_positions_reached()
    test_buy_of_symbol_outside_watchlist_is_always_skipped()
    test_sell_is_allowed_for_a_held_symbol_outside_the_current_watchlist()
    test_low_confidence_decision_is_skipped_when_below_strategy_floor()
    test_hold_never_trades()
    test_sell_cannot_exceed_current_position()
    test_tiny_trade_is_skipped_as_below_minimum()
    test_run_trading_cycle_refuses_mock_market_data()
    test_run_trading_cycle_refuses_when_a_held_symbols_quote_is_missing()
    test_run_trading_cycle_succeeds_end_to_end_with_real_shaped_inputs()
    print("\nAll trading_cycle.py offline tests passed.")
