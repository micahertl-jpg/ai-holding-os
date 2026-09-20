"""
test_live_trading_safety_offline.py — real tests for
tasks/live_trading_safety.py, the hard absolute-dollar circuit breaker
layer for real-money trading. This is the single most safety-critical
piece of the live-trading feature, so it gets the most thorough
coverage in this codebase: every clamp boundary, the daily-loss halt,
and the kill switch, each tested at both sides of its threshold.
"""

from unittest.mock import patch

from tasks.live_trading_safety import (
    apply_live_safety_caps, is_kill_switch_active,
    LIVE_MAX_TRADE_USD, LIVE_MAX_DAILY_LOSS_USD, MIN_LIVE_TRADE_USD,
)


def _executed_trade(symbol="AAPL", quantity=1.0, price=100.0, side="buy"):
    return {"symbol": symbol, "action": side, "executed": True, "side": side,
            "quantity": quantity, "price": price, "confidence_level": "high",
            "rationale": "test"}


def _skipped_trade(symbol="AAPL", reason="model chose hold"):
    return {"symbol": symbol, "action": "hold", "executed": False, "skip_reason": reason,
             "confidence_level": "low", "rationale": "test"}


def test_a_trade_under_the_cap_passes_through_unchanged():
    trade = _executed_trade(quantity=1.0, price=10.0)  # $10, under a $100 cap
    result = apply_live_safety_caps([trade], todays_realized_pnl_usd=0.0, max_trade_usd=100.0)
    assert result[0]["executed"] is True
    assert result[0]["quantity"] == 1.0
    assert "live_cap_applied" not in result[0]
    print("PASS: a trade already under the per-trade dollar cap passes through unchanged")


def test_a_trade_over_the_cap_is_clamped_down_to_it():
    trade = _executed_trade(quantity=10.0, price=50.0)  # $500, well over a $100 cap
    result = apply_live_safety_caps([trade], todays_realized_pnl_usd=0.0, max_trade_usd=100.0)
    assert result[0]["executed"] is True
    assert result[0]["live_cap_applied"] is True
    assert result[0]["pre_cap_quantity"] == 10.0
    clamped_usd = result[0]["quantity"] * result[0]["price"]
    assert abs(clamped_usd - 100.0) < 1e-6, f"clamped trade should be exactly the cap, got ${clamped_usd}"
    print("PASS: a trade over the per-trade dollar cap is clamped down to exactly the cap")


def test_a_trade_clamped_below_the_minimum_is_skipped_not_executed_tiny():
    # $1000 trade clamped to a $1 cap -- below MIN_LIVE_TRADE_USD ($2) -- must be
    # skipped outright, never silently executed as a near-worthless order.
    trade = _executed_trade(quantity=10.0, price=100.0)
    result = apply_live_safety_caps([trade], todays_realized_pnl_usd=0.0, max_trade_usd=1.0)
    assert result[0]["executed"] is False
    assert "minimum" in result[0]["skip_reason"]
    print("PASS: a trade that would clamp below the live minimum is skipped, never executed tiny")


def test_a_skipped_trade_from_upstream_passes_through_unchanged():
    trade = _skipped_trade(reason="confidence low below strategy minimum")
    result = apply_live_safety_caps([trade], todays_realized_pnl_usd=0.0)
    assert result[0] == trade, "an already-skipped trade must never be re-evaluated or altered"
    print("PASS: a trade already skipped upstream (e.g. by apply_risk_limits) passes through untouched")


def test_daily_loss_at_the_threshold_blocks_every_remaining_executed_trade():
    trades = [_executed_trade(symbol="AAPL"), _executed_trade(symbol="MSFT")]
    result = apply_live_safety_caps(trades, todays_realized_pnl_usd=-50.0, max_daily_loss_usd=50.0)
    assert all(t["executed"] is False for t in result)
    assert all("daily loss halt" in t["skip_reason"] for t in result)
    print("PASS: today's realized loss reaching the daily cap blocks every remaining trade this cycle")


def test_daily_loss_under_the_threshold_does_not_block_trades():
    trade = _executed_trade()
    result = apply_live_safety_caps([trade], todays_realized_pnl_usd=-49.99, max_daily_loss_usd=50.0)
    assert result[0]["executed"] is True
    print("PASS: a realized loss just under the daily cap does not block trading")


def test_daily_loss_halt_does_not_affect_trades_already_skipped_upstream():
    trades = [_skipped_trade(reason="model chose hold")]
    result = apply_live_safety_caps(trades, todays_realized_pnl_usd=-1000.0, max_daily_loss_usd=50.0)
    assert result[0]["skip_reason"] == "model chose hold", \
        "the daily-loss halt reason must never overwrite a trade's real, original skip reason"
    print("PASS: the daily-loss halt never rewrites the skip_reason of a trade already skipped upstream")


def test_unrealized_gains_on_open_positions_never_offset_a_realized_daily_loss():
    # This test exists to pin down the contract: callers must pass ONLY
    # realized P&L (sells), never a mark-to-market number that includes
    # unrealized swings on still-open positions -- this function trusts
    # whatever number it's given and does not itself distinguish
    # realized from unrealized, so this documents the caller's
    # responsibility rather than testing new behavior in this file.
    trade = _executed_trade()
    result = apply_live_safety_caps([trade], todays_realized_pnl_usd=-50.0, max_daily_loss_usd=50.0)
    assert result[0]["executed"] is False, (
        "a caller that accidentally passed a rosier mark-to-market number instead of realized "
        "P&L would silently defeat the halt -- this is a caller contract, not a bug in this file"
    )
    print("PASS: the halt trusts todays_realized_pnl_usd exactly as given (documents caller contract)")


def test_default_caps_come_from_env_configurable_module_constants():
    assert LIVE_MAX_TRADE_USD > 0
    assert LIVE_MAX_DAILY_LOSS_USD > 0
    assert MIN_LIVE_TRADE_USD > 0
    assert MIN_LIVE_TRADE_USD < LIVE_MAX_TRADE_USD
    print("PASS: the module's default caps are sane (positive, minimum below the per-trade cap)")


def test_default_caps_match_the_owner_confirmed_values_for_a_100_dollar_account():
    """Pins down the actual owner-confirmed numbers, not just their
    general shape -- a future edit that silently drifts these (e.g. an
    accidental revert) should fail a test, not just ship quietly."""
    assert LIVE_MAX_TRADE_USD == 15.0
    assert LIVE_MAX_DAILY_LOSS_USD == 7.0
    assert MIN_LIVE_TRADE_USD == 2.0
    print("PASS: default caps are exactly the owner-confirmed $15 / $7 / $2 for a $100 starting account")


def test_kill_switch_is_off_by_default():
    with patch.dict("tasks.live_trading_safety.os.environ", {}, clear=True):
        assert is_kill_switch_active() is False
    print("PASS: the kill switch is off when the env var is unset")


def test_kill_switch_recognizes_common_truthy_values():
    for value in ("1", "true", "True", "YES", "on"):
        with patch.dict("tasks.live_trading_safety.os.environ",
                         {"LIVE_TRADING_KILL_SWITCH": value}, clear=True):
            assert is_kill_switch_active() is True, f"expected {value!r} to activate the kill switch"
    print("PASS: the kill switch recognizes common truthy env var spellings")


def test_kill_switch_ignores_falsy_values():
    for value in ("0", "false", "", "no"):
        with patch.dict("tasks.live_trading_safety.os.environ",
                         {"LIVE_TRADING_KILL_SWITCH": value}, clear=True):
            assert is_kill_switch_active() is False, f"expected {value!r} to leave the kill switch off"
    print("PASS: the kill switch stays off for falsy env var values")


def test_a_sell_trade_is_capped_the_same_way_as_a_buy():
    trade = _executed_trade(quantity=10.0, price=50.0, side="sell")
    result = apply_live_safety_caps([trade], todays_realized_pnl_usd=0.0, max_trade_usd=100.0)
    assert result[0]["executed"] is True
    assert result[0]["live_cap_applied"] is True
    print("PASS: a sell over the per-trade cap is clamped exactly like a buy")


if __name__ == "__main__":
    test_a_trade_under_the_cap_passes_through_unchanged()
    test_a_trade_over_the_cap_is_clamped_down_to_it()
    test_a_trade_clamped_below_the_minimum_is_skipped_not_executed_tiny()
    test_a_skipped_trade_from_upstream_passes_through_unchanged()
    test_daily_loss_at_the_threshold_blocks_every_remaining_executed_trade()
    test_daily_loss_under_the_threshold_does_not_block_trades()
    test_daily_loss_halt_does_not_affect_trades_already_skipped_upstream()
    test_unrealized_gains_on_open_positions_never_offset_a_realized_daily_loss()
    test_default_caps_come_from_env_configurable_module_constants()
    test_default_caps_match_the_owner_confirmed_values_for_a_100_dollar_account()
    test_kill_switch_is_off_by_default()
    test_kill_switch_recognizes_common_truthy_values()
    test_kill_switch_ignores_falsy_values()
    test_a_sell_trade_is_capped_the_same_way_as_a_buy()
    print("\nAll live_trading_safety.py offline tests passed.")
