"""
test_market_data_offline.py — real tests for market_data.py that don't
need network access: the response-shape parsing/validation logic (via a
fake urlopen) and MockMarketDataClient's contract.
"""

import json
from unittest.mock import patch, MagicMock

import market_data


def _fake_response(payload_dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload_dict).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_get_quote_parses_a_real_shaped_response():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Global Quote": {"01. symbol": "AAPL", "05. price": "231.50",
                                 "07. latest trading day": "2026-01-02"}}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        quote = client.get_quote("AAPL")
    assert quote == {"symbol": "AAPL", "price": 231.50, "as_of": "2026-01-02", "mock": False}
    print("PASS: get_quote parses a real-shaped Alpha Vantage response correctly")


def test_get_quote_raises_on_rate_limit_note():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is ..."}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        try:
            client.get_quote("AAPL")
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "rate-limit" in str(e)
    print("PASS: a rate-limit 'Note' response raises MarketDataError instead of being "
          "silently parsed as if it were quote data")


def test_get_quote_raises_on_missing_price_field():
    client = market_data.AlphaVantageClient(api_key="fake-key")
    payload = {"Global Quote": {}}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        try:
            client.get_quote("BADSYM")
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "no '05. price' field" in str(e)
    print("PASS: an empty Global Quote (e.g. invalid symbol) raises MarketDataError, "
          "never a fabricated price")


def test_client_refuses_to_construct_without_api_key():
    with patch.dict("os.environ", {}, clear=True):
        try:
            market_data.AlphaVantageClient(api_key=None)
            assert False, "expected MarketDataError"
        except market_data.MarketDataError as e:
            assert "ALPHAVANTAGE_API_KEY" in str(e)
    print("PASS: AlphaVantageClient refuses to construct without an API key -- never "
          "silently falls back to fabricated data")


def test_mock_client_is_clearly_labeled():
    client = market_data.MockMarketDataClient(prices={"AAPL": 123.45})
    quote = client.get_quote("AAPL")
    assert quote["price"] == 123.45
    assert quote["mock"] is True
    default_quote = market_data.MockMarketDataClient().get_quote("UNKNOWN")
    assert default_quote["mock"] is True
    print("PASS: MockMarketDataClient tags every quote mock=True so callers can refuse "
          "to trade on it")


def test_get_default_client_picks_real_client_only_with_a_key():
    with patch.dict("os.environ", {"ALPHAVANTAGE_API_KEY": "real-key"}, clear=True):
        assert isinstance(market_data.get_default_client(), market_data.AlphaVantageClient)
    with patch.dict("os.environ", {}, clear=True):
        assert isinstance(market_data.get_default_client(), market_data.MockMarketDataClient)
    print("PASS: get_default_client() returns a real client only when a key is configured, "
          "an explicitly-labeled mock otherwise")


if __name__ == "__main__":
    test_get_quote_parses_a_real_shaped_response()
    test_get_quote_raises_on_rate_limit_note()
    test_get_quote_raises_on_missing_price_field()
    test_client_refuses_to_construct_without_api_key()
    test_mock_client_is_clearly_labeled()
    test_get_default_client_picks_real_client_only_with_a_key()
    print("\nAll market_data.py offline tests passed.")
