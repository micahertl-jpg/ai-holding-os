"""
test_alpaca_client_offline.py — real tests for alpaca_client.py that
don't need network access: the safety-critical paper/live endpoint
default (via a fake urlopen for the real client, no mocking needed for
the pure validation/default logic), and MockAlpacaClient's full
buy/sell/insufficient-funds contract.
"""

import json
from unittest.mock import patch, MagicMock

import alpaca_client
from alpaca_client import AlpacaClient, AlpacaError, MockAlpacaClient, get_default_client


def _fake_response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_client_refuses_to_construct_without_credentials():
    try:
        AlpacaClient(api_key=None, api_secret=None)
        raise AssertionError("expected AlpacaError")
    except AlpacaError as e:
        assert "ALPACA_API_KEY" in str(e)
    print("PASS: AlpacaClient refuses to construct without both credentials")


def test_default_base_url_is_paper_not_live():
    """The single most important safety property of this whole file:
    credentials alone are never enough to reach real money. Without an
    explicit ALPACA_BASE_URL, the client must default to Alpaca's own
    paper-trading endpoint."""
    with patch.dict("alpaca_client.os.environ", {}, clear=True):
        client = AlpacaClient(api_key="k", api_secret="s")
    assert client.base_url == alpaca_client.PAPER_BASE_URL
    assert client.is_paper is True
    print("PASS: with no ALPACA_BASE_URL set, the client defaults to the paper endpoint")


def test_explicit_live_base_url_is_recognized_as_not_paper():
    client = AlpacaClient(api_key="k", api_secret="s", base_url=alpaca_client.LIVE_BASE_URL)
    assert client.is_paper is False
    print("PASS: an explicitly-configured live base URL correctly reports is_paper=False")


def test_get_account_parses_a_real_shaped_response():
    client = AlpacaClient(api_key="k", api_secret="s")
    payload = {"cash": "483.21", "equity": "512.90", "buying_power": "483.21", "status": "ACTIVE"}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        account = client.get_account()
    assert account == {"cash": 483.21, "equity": 512.90, "buying_power": 483.21,
                        "status": "ACTIVE", "is_paper": True}
    print("PASS: get_account parses a real-shaped Alpaca account response correctly")


def test_get_positions_parses_a_real_shaped_response():
    client = AlpacaClient(api_key="k", api_secret="s")
    payload = [{"symbol": "AAPL", "qty": "1.5", "avg_entry_price": "200.00", "market_value": "310.00"}]
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        positions = client.get_positions()
    assert positions == [{"symbol": "AAPL", "quantity": 1.5, "avg_entry_price": 200.00,
                           "market_value": 310.00}]
    print("PASS: get_positions parses a real-shaped Alpaca positions response correctly")


def test_place_order_rejects_invalid_side_before_any_network_call():
    client = AlpacaClient(api_key="k", api_secret="s")
    with patch("urllib.request.urlopen") as mock_open:
        try:
            client.place_order("AAPL", "hold", 50.0)
            raise AssertionError("expected AlpacaError")
        except AlpacaError as e:
            assert "side" in str(e)
    assert mock_open.call_count == 0, "an invalid order must never reach the network"
    print("PASS: place_order validates side before ever calling the broker API")


def test_place_order_rejects_non_positive_notional_before_any_network_call():
    client = AlpacaClient(api_key="k", api_secret="s")
    with patch("urllib.request.urlopen") as mock_open:
        for bad in (0, -10.0):
            try:
                client.place_order("AAPL", "buy", bad)
                raise AssertionError("expected AlpacaError")
            except AlpacaError:
                pass
    assert mock_open.call_count == 0
    print("PASS: place_order validates notional_usd before ever calling the broker API")


def test_http_error_from_alpaca_raises_alpaca_error_with_detail():
    import urllib.error
    client = AlpacaClient(api_key="k", api_secret="s")
    err_body = MagicMock()
    err_body.read.return_value = b'{"code": 40310000, "message": "insufficient buying power"}'
    http_err = urllib.error.HTTPError(
        url="https://paper-api.alpaca.markets/v2/orders", code=403, msg="Forbidden",
        hdrs=None, fp=err_body,
    )
    with patch("urllib.request.urlopen", side_effect=http_err):
        try:
            client.place_order("AAPL", "buy", 50.0)
            raise AssertionError("expected AlpacaError")
        except AlpacaError as e:
            assert "403" in str(e)
            assert "insufficient buying power" in str(e)
    print("PASS: a rejected order surfaces Alpaca's real error detail, never a fabricated success")


def test_get_default_client_never_silently_mocks():
    with patch.dict("alpaca_client.os.environ", {}, clear=True):
        try:
            get_default_client()
            raise AssertionError("expected AlpacaError")
        except AlpacaError as e:
            assert "ALPACA_API_KEY" in str(e)
    print("PASS: get_default_client() refuses to run rather than silently falling back to a mock")


def test_mock_client_buy_reduces_cash_and_opens_a_position():
    client = MockAlpacaClient(cash=1000.0, quote_prices={"AAPL": 200.0})
    order = client.place_order("AAPL", "buy", 200.0)
    assert order["status"] == "filled"
    assert client.cash == 800.0
    positions = client.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert abs(positions[0]["quantity"] - 1.0) < 1e-9
    print("PASS: MockAlpacaClient buy reduces cash and opens a position")


def test_mock_client_sell_increases_cash_and_closes_a_position():
    client = MockAlpacaClient(cash=800.0, positions={"AAPL": {"quantity": 1.0, "avg_entry_price": 200.0}},
                               quote_prices={"AAPL": 220.0})
    order = client.place_order("AAPL", "sell", 220.0)
    assert order["status"] == "filled"
    assert client.cash == 1020.0
    assert client.get_positions() == [], "a fully-closed position must not appear as open"
    print("PASS: MockAlpacaClient sell increases cash and closes the position")


def test_mock_client_refuses_a_buy_beyond_available_cash():
    client = MockAlpacaClient(cash=100.0, quote_prices={"AAPL": 200.0})
    try:
        client.place_order("AAPL", "buy", 500.0)
        raise AssertionError("expected AlpacaError")
    except AlpacaError as e:
        assert "insufficient buying power" in str(e)
    assert client.cash == 100.0, "a rejected order must never move cash"
    print("PASS: MockAlpacaClient refuses a buy beyond available cash, cash left untouched")


def test_mock_client_refuses_a_sell_beyond_held_quantity():
    client = MockAlpacaClient(cash=0.0, positions={"AAPL": {"quantity": 1.0, "avg_entry_price": 200.0}},
                               quote_prices={"AAPL": 200.0})
    try:
        client.place_order("AAPL", "sell", 1000.0)  # far more than the 1 share held
        raise AssertionError("expected AlpacaError")
    except AlpacaError as e:
        assert "insufficient position" in str(e)
    print("PASS: MockAlpacaClient refuses a sell beyond the held quantity")


def test_mock_client_get_order_round_trips_a_placed_order():
    client = MockAlpacaClient(cash=1000.0, quote_prices={"AAPL": 200.0})
    placed = client.place_order("AAPL", "buy", 200.0)
    fetched = client.get_order(placed["id"])
    assert fetched == placed
    try:
        client.get_order("does-not-exist")
        raise AssertionError("expected AlpacaError")
    except AlpacaError:
        pass
    print("PASS: MockAlpacaClient get_order round-trips a real order id and rejects an unknown one")


def test_mock_client_is_paper_is_always_true():
    assert MockAlpacaClient().is_paper is True
    print("PASS: MockAlpacaClient always reports is_paper=True — it can never represent real money")


if __name__ == "__main__":
    test_client_refuses_to_construct_without_credentials()
    test_default_base_url_is_paper_not_live()
    test_explicit_live_base_url_is_recognized_as_not_paper()
    test_get_account_parses_a_real_shaped_response()
    test_get_positions_parses_a_real_shaped_response()
    test_place_order_rejects_invalid_side_before_any_network_call()
    test_place_order_rejects_non_positive_notional_before_any_network_call()
    test_http_error_from_alpaca_raises_alpaca_error_with_detail()
    test_get_default_client_never_silently_mocks()
    test_mock_client_buy_reduces_cash_and_opens_a_position()
    test_mock_client_sell_increases_cash_and_closes_a_position()
    test_mock_client_refuses_a_buy_beyond_available_cash()
    test_mock_client_refuses_a_sell_beyond_held_quantity()
    test_mock_client_get_order_round_trips_a_placed_order()
    test_mock_client_is_paper_is_always_true()
    print("\nAll alpaca_client.py offline tests passed.")
