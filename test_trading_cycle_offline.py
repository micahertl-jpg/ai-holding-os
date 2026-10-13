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
from tasks.trading_common import (
    DEFAULT_STRATEGY_PARAMS, validate_parameters, StrategyParameterError,
    compute_atr_pct, compute_average_atr_pct, MIN_ATR_BARS,
)
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


def test_validate_parameters_defaults_stop_loss_and_take_profit_to_none_when_omitted():
    # A strategy_params dict saved before this feature existed has
    # neither key at all -- must still validate (they're OPTIONAL, not
    # required) and come back explicitly disabled, not missing.
    old = {k: v for k, v in DEFAULT_STRATEGY_PARAMS.items()
           if k not in ("stop_loss_pct", "take_profit_pct")}
    validated = validate_parameters(old)
    assert validated["stop_loss_pct"] is None
    assert validated["take_profit_pct"] is None
    print("PASS: validate_parameters defaults stop_loss_pct/take_profit_pct to None (disabled) "
          "for an older params dict that predates this feature, rather than erroring")


def test_validate_parameters_accepts_real_stop_loss_and_take_profit_values():
    params = dict(DEFAULT_STRATEGY_PARAMS)
    params["stop_loss_pct"] = 0.08
    params["take_profit_pct"] = 0.25
    validated = validate_parameters(params)
    assert validated["stop_loss_pct"] == 0.08
    assert validated["take_profit_pct"] == 0.25
    print("PASS: validate_parameters accepts real stop_loss_pct/take_profit_pct values")


def test_validate_parameters_rejects_stop_loss_over_absolute_ceiling():
    bad = dict(DEFAULT_STRATEGY_PARAMS)
    bad["stop_loss_pct"] = 0.9  # above ABSOLUTE_MAX_STOP_LOSS_PCT (0.5)
    try:
        validate_parameters(bad)
        assert False, "expected StrategyParameterError"
    except StrategyParameterError:
        pass
    print("PASS: validate_parameters rejects a stop_loss_pct above its absolute ceiling")


def test_validate_parameters_rejects_take_profit_over_absolute_ceiling():
    bad = dict(DEFAULT_STRATEGY_PARAMS)
    bad["take_profit_pct"] = 10.0  # above ABSOLUTE_MAX_TAKE_PROFIT_PCT (5.0)
    try:
        validate_parameters(bad)
        assert False, "expected StrategyParameterError"
    except StrategyParameterError:
        pass
    print("PASS: validate_parameters rejects a take_profit_pct above its absolute ceiling")


def test_validate_parameters_rejects_non_numeric_stop_loss():
    bad = dict(DEFAULT_STRATEGY_PARAMS)
    bad["stop_loss_pct"] = "8%"
    try:
        validate_parameters(bad)
        assert False, "expected StrategyParameterError"
    except StrategyParameterError:
        pass
    print("PASS: validate_parameters rejects a non-numeric stop_loss_pct")


def test_validate_parameters_accepts_and_defaults_atr_multiples():
    params = dict(DEFAULT_STRATEGY_PARAMS)
    params["auto_stop_loss_atr_multiple"] = 2.0
    params["auto_take_profit_atr_multiple"] = 3.5
    validated = validate_parameters(params)
    assert validated["auto_stop_loss_atr_multiple"] == 2.0
    assert validated["auto_take_profit_atr_multiple"] == 3.5

    old = {k: v for k, v in DEFAULT_STRATEGY_PARAMS.items()
           if k not in ("auto_stop_loss_atr_multiple", "auto_take_profit_atr_multiple")}
    assert validate_parameters(old)["auto_stop_loss_atr_multiple"] is None
    print("PASS: validate_parameters accepts real ATR multiples and defaults them to None "
          "(disabled) when omitted entirely")


def test_validate_parameters_rejects_atr_multiple_over_absolute_ceiling():
    bad = dict(DEFAULT_STRATEGY_PARAMS)
    bad["auto_stop_loss_atr_multiple"] = 20.0  # above ABSOLUTE_MAX_ATR_MULTIPLE (10.0)
    try:
        validate_parameters(bad)
        assert False, "expected StrategyParameterError"
    except StrategyParameterError:
        pass
    print("PASS: validate_parameters rejects an auto_stop_loss_atr_multiple above its "
          "absolute ceiling")


# ---------------------------------------------------------------------
# compute_atr_pct / compute_average_atr_pct — real volatility math
# behind the automatic (ATR-based) stop-loss/take-profit feature
# ---------------------------------------------------------------------

def _atr_bar(date, high, low, close, mock=False):
    return {"date": date, "open": close, "high": high, "low": low, "close": close,
            "volume": 1000000, "mock": mock}


def test_compute_atr_pct_matches_a_hand_computed_value():
    # Every day's true range is exactly 4.0 (constructed so H-L and both
    # gap-based measures agree) across 6 days -- ATR = 4.0, latest close
    # 110.0, so ATR% must be exactly 4/110.
    bars = [
        _atr_bar("2026-01-01", 102, 98, 100),
        _atr_bar("2026-01-02", 104, 100, 102),
        _atr_bar("2026-01-03", 106, 102, 104),
        _atr_bar("2026-01-04", 108, 104, 106),
        _atr_bar("2026-01-05", 110, 106, 108),
        _atr_bar("2026-01-06", 112, 108, 110),
    ]
    atr_pct = compute_atr_pct(bars)
    assert abs(atr_pct - (4.0 / 110.0)) < 1e-9, atr_pct
    print("PASS: compute_atr_pct matches a hand-computed Average True Range percentage")


def test_compute_atr_pct_uses_the_gap_based_true_range_not_just_high_minus_low():
    # A gap UP overnight: day 2 trades in a narrow 101-103 range (H-L=2)
    # but opens/gaps from day 1's close of 90 -- the TRUE range must
    # capture that 13-point gap (|103 - 90|), not just the narrow 2-point
    # intraday range, or a volatility measure built on it would badly
    # understate real risk around gaps.
    bars = [
        _atr_bar("2026-01-01", 92, 88, 90),
        _atr_bar("2026-01-02", 103, 101, 102),
        _atr_bar("2026-01-03", 104, 100, 102),
        _atr_bar("2026-01-04", 104, 100, 102),
        _atr_bar("2026-01-05", 104, 100, 102),
    ]
    atr_pct = compute_atr_pct(bars)
    # day1 TR=4, day2 TR=max(2, |103-90|=13, |101-90|=11)=13, days 3-5 TR=4 each
    expected_atr = (4 + 13 + 4 + 4 + 4) / 5
    assert abs(atr_pct - expected_atr / 102.0) < 1e-9, atr_pct
    print("PASS: compute_atr_pct's true range captures an overnight gap, not just the "
          "intraday high-low range")


def test_compute_atr_pct_refuses_with_too_few_bars():
    bars = [_atr_bar(f"2026-01-0{i}", 102, 98, 100) for i in range(1, MIN_ATR_BARS)]
    assert len(bars) < MIN_ATR_BARS
    assert compute_atr_pct(bars) is None
    print("PASS: compute_atr_pct refuses (returns None) with fewer than MIN_ATR_BARS real bars")


def test_compute_atr_pct_refuses_mock_bars():
    bars = [_atr_bar(f"2026-01-0{i}", 102, 98, 100, mock=(i == 3)) for i in range(1, 7)]
    assert compute_atr_pct(bars) is None
    print("PASS: compute_atr_pct refuses to compute a volatility figure from mock historical "
          "data, same rule run_trading_cycle already applies to live quotes")


def test_compute_average_atr_pct_averages_across_symbols():
    calm = [_atr_bar(f"2026-01-0{i}", 101, 99, 100) for i in range(1, 7)]  # TR=2 every day
    volatile = [_atr_bar(f"2026-01-0{i}", 110, 90, 100) for i in range(1, 7)]  # TR=20 every day
    avg = compute_average_atr_pct({"CALM": calm, "VOLATILE": volatile})
    expected = ((2 / 100.0) + (20 / 100.0)) / 2
    assert abs(avg - expected) < 1e-9, avg
    print("PASS: compute_average_atr_pct simple-averages each symbol's real ATR% across the "
          "whole watchlist")


def test_compute_average_atr_pct_refuses_if_any_symbol_cannot_be_computed():
    enough = [_atr_bar(f"2026-01-0{i}", 101, 99, 100) for i in range(1, 7)]
    too_few = [_atr_bar("2026-01-01", 101, 99, 100)]
    assert compute_average_atr_pct({"AAPL": enough, "MSFT": too_few}) is None
    print("PASS: compute_average_atr_pct refuses (never a partial/best-effort average) if even "
          "one watchlist symbol's volatility can't be computed")


def test_compute_average_atr_pct_refuses_with_an_empty_watchlist():
    assert compute_average_atr_pct({}) is None
    print("PASS: compute_average_atr_pct refuses with no symbols given at all")


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
    print("PASS: a trade clamped below MIN_TRADE_USD is skipped rather than executed at a "
          "meaningless size")


# ---------------------------------------------------------------------
# apply_risk_limits — stop-loss / take-profit (code-enforced, never a
# model suggestion)
# ---------------------------------------------------------------------

def test_stop_loss_force_closes_a_losing_position_even_when_the_model_says_hold():
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "stop_loss_pct": 0.08})
    # Bought at $100, now $90 -- a real 10% unrealized loss, past the 8% stop.
    quotes = {"AAPL": {"price": 90.0, "as_of": "x", "mock": False}}
    positions = [{"symbol": "AAPL", "quantity": 10.0, "avg_cost_usd": 100.0}]
    decisions = [{"symbol": "AAPL", "action": "hold", "confidence_level": "high",
                  "size_pct": 0.0, "rationale": "looks fine to me"}]
    result = apply_risk_limits(decisions, 0.0, positions, quotes, params)
    assert len(result) == 1
    assert result[0]["executed"] is True
    assert result[0]["side"] == "sell"
    assert result[0]["quantity"] == 10.0, "a stop-loss closes the FULL position, never a partial amount"
    assert "stop-loss" in result[0]["rationale"]
    print("PASS: a position past its configured stop_loss_pct is force-closed in full, even "
          "though the model itself said hold")


def test_take_profit_force_closes_a_winning_position_even_when_the_model_says_hold():
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "take_profit_pct": 0.20})
    # Bought at $100, now $125 -- a real 25% unrealized gain, past the 20% target.
    quotes = {"AAPL": {"price": 125.0, "as_of": "x", "mock": False}}
    positions = [{"symbol": "AAPL", "quantity": 10.0, "avg_cost_usd": 100.0}]
    decisions = [{"symbol": "AAPL", "action": "hold", "confidence_level": "high",
                  "size_pct": 0.0, "rationale": "let it keep running"}]
    result = apply_risk_limits(decisions, 0.0, positions, quotes, params)
    assert result[0]["executed"] is True
    assert result[0]["side"] == "sell"
    assert result[0]["quantity"] == 10.0
    assert "take-profit" in result[0]["rationale"]
    print("PASS: a position past its configured take_profit_pct is force-closed in full, even "
          "though the model itself said hold")


def test_stop_loss_overrides_an_explicit_buy_decision_too():
    # Not just overriding "hold" -- a stop-loss fires no matter WHAT the
    # model proposed for this symbol, including trying to add to the
    # very position that's blowing past its stop.
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "stop_loss_pct": 0.08})
    quotes = {"AAPL": {"price": 85.0, "as_of": "x", "mock": False}}
    positions = [{"symbol": "AAPL", "quantity": 10.0, "avg_cost_usd": 100.0}]
    decisions = [{"symbol": "AAPL", "action": "buy", "confidence_level": "high",
                  "size_pct": 0.5, "rationale": "buy the dip"}]
    result = apply_risk_limits(decisions, 10000.0, positions, quotes, params)
    assert result[0]["executed"] is True
    assert result[0]["side"] == "sell", "a stop-loss must win over any other proposed action"
    print("PASS: a stop-loss overrides even an explicit buy decision for the same symbol")


def test_stop_loss_bypasses_min_confidence_to_trade():
    # A normal sell this low-confidence would be skipped by the
    # strategy's min_confidence_to_trade floor (medium, by default) --
    # a forced stop-loss is a risk control, not a trade suggestion, so
    # it must never be gated by confidence at all.
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "stop_loss_pct": 0.08})
    quotes = {"AAPL": {"price": 90.0, "as_of": "x", "mock": False}}
    positions = [{"symbol": "AAPL", "quantity": 10.0, "avg_cost_usd": 100.0}]
    decisions = [{"symbol": "AAPL", "action": "hold", "confidence_level": "low",
                  "size_pct": 0.0, "rationale": "not sure"}]
    result = apply_risk_limits(decisions, 0.0, positions, quotes, params)
    assert result[0]["executed"] is True and result[0]["side"] == "sell"
    print("PASS: a forced stop-loss executes regardless of min_confidence_to_trade")


def test_no_forced_close_when_stop_loss_and_take_profit_are_disabled():
    # The default -- both None -- must behave exactly as before this
    # feature existed: a losing position just sits there until the
    # model itself decides to sell it.
    quotes = {"AAPL": {"price": 50.0, "as_of": "x", "mock": False}}  # down 50%
    positions = [{"symbol": "AAPL", "quantity": 10.0, "avg_cost_usd": 100.0}]
    decisions = [{"symbol": "AAPL", "action": "hold", "confidence_level": "high",
                  "size_pct": 0.0, "rationale": "x"}]
    result = apply_risk_limits(decisions, 0.0, positions, quotes, PARAMS)
    assert result[0]["executed"] is False, "with no stop-loss configured, nothing forces a close"
    print("PASS: with stop_loss_pct/take_profit_pct disabled (the default), a losing position "
          "is never force-closed")


def test_stop_loss_does_not_affect_a_symbol_with_no_open_position():
    # A brand-new buy decision for a symbol with no position shouldn't
    # somehow trip the stop-loss check (nothing to protect yet).
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "stop_loss_pct": 0.08})
    quotes = {"AAPL": {"price": 100.0, "as_of": "x", "mock": False}}
    decisions = [{"symbol": "AAPL", "action": "buy", "confidence_level": "high",
                  "size_pct": 0.1, "rationale": "x"}]
    result = apply_risk_limits(decisions, 10000.0, [], quotes, params)
    assert result[0]["executed"] is True and result[0]["side"] == "buy"
    print("PASS: a stop-loss configured but no open position never blocks a normal new buy")


def test_stop_loss_exactly_at_threshold_triggers():
    # Half-open-boundary check: exactly -8% with an 8% stop must fire
    # (<=), not require crossing strictly past it.
    params = validate_parameters({**DEFAULT_STRATEGY_PARAMS, "stop_loss_pct": 0.08})
    quotes = {"AAPL": {"price": 92.0, "as_of": "x", "mock": False}}  # exactly -8% from 100
    positions = [{"symbol": "AAPL", "quantity": 10.0, "avg_cost_usd": 100.0}]
    decisions = [{"symbol": "AAPL", "action": "hold", "confidence_level": "high",
                  "size_pct": 0.0, "rationale": "x"}]
    result = apply_risk_limits(decisions, 0.0, positions, quotes, params)
    assert result[0]["executed"] is True and result[0]["side"] == "sell"
    print("PASS: a loss exactly at the configured stop_loss_pct triggers the stop (inclusive)")


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
          "(no TWELVEDATA_API_KEY configured)")


def test_run_trading_cycle_refuses_when_a_held_symbols_quote_is_missing():
    positions = [{"symbol": "IBM", "quantity": 10.0, "avg_cost_usd": 100.0}]
    market_client = _FakeMarketClient({s: 100.0 for s in PARAMS["watchlist"]}, raise_for=["IBM"])
    try:
        run_trading_cycle(10000.0, positions, PARAMS, "", market_client,
                           _FakeLLMClient('{"decisions": []}'))
        assert False, "expected TradingCycleError"
    except TradingCycleError as e:
        assert "IBM" in str(e)
        # The real underlying reason (e.g. Twelve Data's exact
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
    test_validate_parameters_defaults_stop_loss_and_take_profit_to_none_when_omitted()
    test_validate_parameters_accepts_real_stop_loss_and_take_profit_values()
    test_validate_parameters_rejects_stop_loss_over_absolute_ceiling()
    test_validate_parameters_rejects_take_profit_over_absolute_ceiling()
    test_validate_parameters_rejects_non_numeric_stop_loss()
    test_validate_parameters_accepts_and_defaults_atr_multiples()
    test_validate_parameters_rejects_atr_multiple_over_absolute_ceiling()
    test_compute_atr_pct_matches_a_hand_computed_value()
    test_compute_atr_pct_uses_the_gap_based_true_range_not_just_high_minus_low()
    test_compute_atr_pct_refuses_with_too_few_bars()
    test_compute_atr_pct_refuses_mock_bars()
    test_compute_average_atr_pct_averages_across_symbols()
    test_compute_average_atr_pct_refuses_if_any_symbol_cannot_be_computed()
    test_compute_average_atr_pct_refuses_with_an_empty_watchlist()
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
    test_stop_loss_force_closes_a_losing_position_even_when_the_model_says_hold()
    test_take_profit_force_closes_a_winning_position_even_when_the_model_says_hold()
    test_stop_loss_overrides_an_explicit_buy_decision_too()
    test_stop_loss_bypasses_min_confidence_to_trade()
    test_no_forced_close_when_stop_loss_and_take_profit_are_disabled()
    test_stop_loss_does_not_affect_a_symbol_with_no_open_position()
    test_stop_loss_exactly_at_threshold_triggers()
    test_run_trading_cycle_refuses_mock_market_data()
    test_run_trading_cycle_refuses_when_a_held_symbols_quote_is_missing()
    test_run_trading_cycle_succeeds_end_to_end_with_real_shaped_inputs()
    print("\nAll trading_cycle.py offline tests passed.")
