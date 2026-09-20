"""
alpaca_client.py — real brokerage client for live stock trading, using
only the Python standard library (urllib), same pattern as
market_data.py/stripe_client.py/emailer.py.

SAFETY DEFAULT, READ THIS FIRST: `AlpacaClient` defaults to Alpaca's
PAPER trading endpoint (paper-api.alpaca.markets) unless ALPACA_BASE_URL
is explicitly set to the live one (api.alpaca.markets). This means an
account that has ALPACA_API_KEY/ALPACA_API_SECRET configured but never
explicitly set ALPACA_BASE_URL can NEVER place a real order — it will
always be talking to Alpaca's own paper simulator, which uses the
identical API/response shapes as live trading. Going live is a single,
deliberate env var change the owner makes themselves; it is never the
default, and this client makes that fact checkable at runtime via
`.is_paper`.

This client places REAL orders when pointed at the live endpoint with
real credentials — it is capable of moving real money the moment it's
configured that way. Nothing that imports this module should ever call
place_order() without having already run the order through
tasks/trading_cycle.py's apply_risk_limits() AND
tasks/live_trading_safety.py's apply_live_safety_caps() first — those
are the actual safety guarantee; this file is just a thin, honest
transport to the broker, same "model proposes, code disposes" split
as everywhere else trading-related in this codebase.
"""

import json
import os
import urllib.request
import urllib.error

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
REQUEST_TIMEOUT_SECONDS = 20


class AlpacaError(Exception):
    pass


class AlpacaClient:
    """Thin real client over Alpaca's REST API (Trading API v2).
    `.is_paper` tells the caller, definitively, whether this instance
    can ever touch real money — always check it before treating a
    successful call as proof of anything about real capital."""

    def __init__(self, api_key: str = None, api_secret: str = None, base_url: str = None):
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY")
        self.api_secret = api_secret or os.environ.get("ALPACA_API_SECRET")
        if not self.api_key or not self.api_secret:
            raise AlpacaError(
                "ALPACA_API_KEY and ALPACA_API_SECRET must both be set. Refusing to "
                "proceed — this system never fabricates a broker connection."
            )
        # Defaults to the PAPER endpoint -- see module docstring. Only an
        # explicit ALPACA_BASE_URL (or constructor arg) pointed at
        # api.alpaca.markets can ever make this a live-money client.
        self.base_url = (base_url or os.environ.get("ALPACA_BASE_URL") or PAPER_BASE_URL).rstrip("/")

    @property
    def is_paper(self) -> bool:
        return self.base_url != LIVE_BASE_URL

    def _request(self, method: str, path: str, body: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.api_secret,
                "Content-Type": "application/json",
                "User-Agent": "ai-holding-os-trading/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise AlpacaError(f"Alpaca API HTTP {e.code} on {method} {path}: {detail}") from e
        except urllib.error.URLError as e:
            raise AlpacaError(f"network error calling Alpaca API ({method} {path}): {e}") from e

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise AlpacaError(f"non-JSON response from Alpaca API ({method} {path}): {e}") from e

    def get_account(self) -> dict:
        """Returns the REAL account state — {"cash", "equity",
        "buying_power", "status", "is_paper"} — fetched fresh from
        Alpaca every call. This is the source of truth for live
        trading; nothing here is derived from a locally-cached ledger,
        so it can never drift out of sync with what the broker actually
        holds."""
        data = self._request("GET", "/v2/account")
        try:
            return {
                "cash": float(data["cash"]),
                "equity": float(data["equity"]),
                "buying_power": float(data["buying_power"]),
                "status": data["status"],
                "is_paper": self.is_paper,
            }
        except (KeyError, TypeError, ValueError) as e:
            raise AlpacaError(f"unexpected account response shape from Alpaca: {data}") from e

    def get_positions(self) -> list:
        """Returns real open positions — [{"symbol", "quantity",
        "avg_entry_price", "market_value"}, ...] — fetched fresh from
        Alpaca every call."""
        data = self._request("GET", "/v2/positions")
        if not isinstance(data, list):
            raise AlpacaError(f"expected a list of positions from Alpaca, got: {data}")
        positions = []
        try:
            for p in data:
                positions.append({
                    "symbol": p["symbol"],
                    "quantity": float(p["qty"]),
                    "avg_entry_price": float(p["avg_entry_price"]),
                    "market_value": float(p["market_value"]),
                })
        except (KeyError, TypeError, ValueError) as e:
            raise AlpacaError(f"unexpected position response shape from Alpaca: {data}") from e
        return positions

    def place_order(self, symbol: str, side: str, notional_usd: float,
                     order_type: str = "market", time_in_force: str = "day") -> dict:
        """Places a REAL order (a real order against real Alpaca funds
        if self.is_paper is False) sized by dollar amount (notional),
        not share count — matches how tasks/trading_cycle.py already
        sizes every decision (a fraction of cash/position value), so no
        separate share-count rounding logic is needed here. Returns the
        raw order dict Alpaca gives back (includes its own id for
        get_order() polling). Raises AlpacaError on any rejection —
        never returns a fabricated success."""
        if side not in ("buy", "sell"):
            raise AlpacaError(f"side must be 'buy' or 'sell', got {side!r}")
        if notional_usd <= 0:
            raise AlpacaError(f"notional_usd must be positive, got {notional_usd}")
        body = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
            "notional": f"{notional_usd:.2f}",
        }
        return self._request("POST", "/v2/orders", body=body)

    def get_order(self, order_id: str) -> dict:
        """Polls a previously-placed order's current status/fill info."""
        return self._request("GET", f"/v2/orders/{order_id}")


class MockAlpacaClient:
    """Explicit stand-in for offline tests and any environment with no
    real Alpaca credentials — never silently used as a substitute for a
    missing real connection anywhere live trading would actually run
    (get_default_client() below refuses instead). Every response is
    clearly synthetic; `is_paper` is always True since a mock can never
    represent a live-money connection."""

    is_paper = True

    def __init__(self, cash: float = 1000.0, positions: dict = None,
                 quote_prices: dict = None, default_price: float = 100.0):
        self.cash = cash
        self.positions = positions or {}  # symbol -> {"quantity", "avg_entry_price"}
        self.quote_prices = quote_prices or {}
        self.default_price = default_price
        self.orders = []

    def _price(self, symbol):
        return self.quote_prices.get(symbol, self.default_price)

    def get_account(self) -> dict:
        equity = self.cash + sum(
            p["quantity"] * self._price(sym) for sym, p in self.positions.items()
        )
        return {"cash": self.cash, "equity": equity, "buying_power": self.cash,
                "status": "ACTIVE", "is_paper": True}

    def get_positions(self) -> list:
        return [
            {"symbol": sym, "quantity": p["quantity"], "avg_entry_price": p["avg_entry_price"],
             "market_value": p["quantity"] * self._price(sym)}
            for sym, p in self.positions.items() if p["quantity"] > 0
        ]

    def place_order(self, symbol: str, side: str, notional_usd: float,
                     order_type: str = "market", time_in_force: str = "day") -> dict:
        if side not in ("buy", "sell"):
            raise AlpacaError(f"side must be 'buy' or 'sell', got {side!r}")
        if notional_usd <= 0:
            raise AlpacaError(f"notional_usd must be positive, got {notional_usd}")
        price = self._price(symbol)
        qty = notional_usd / price
        existing = self.positions.get(symbol, {"quantity": 0.0, "avg_entry_price": 0.0})
        if side == "buy":
            if notional_usd > self.cash:
                raise AlpacaError(f"insufficient buying power: ${notional_usd:.2f} requested, "
                                   f"${self.cash:.2f} available")
            new_qty = existing["quantity"] + qty
            existing["avg_entry_price"] = (
                (existing["quantity"] * existing["avg_entry_price"] + qty * price) / new_qty
                if new_qty > 0 else 0.0
            )
            existing["quantity"] = new_qty
            self.cash -= notional_usd
        else:
            if qty > existing["quantity"] + 1e-9:
                raise AlpacaError(f"insufficient position: selling {qty:.4f} {symbol}, "
                                   f"only {existing['quantity']:.4f} held")
            existing["quantity"] -= qty
            self.cash += notional_usd
        self.positions[symbol] = existing
        order = {
            "id": f"mock-order-{len(self.orders) + 1}", "symbol": symbol, "side": side,
            "notional": f"{notional_usd:.2f}", "filled_qty": f"{qty:.6f}",
            "filled_avg_price": f"{price:.2f}", "status": "filled",
        }
        self.orders.append(order)
        return order

    def get_order(self, order_id: str) -> dict:
        for o in self.orders:
            if o["id"] == order_id:
                return o
        raise AlpacaError(f"no such mock order: {order_id!r}")


def get_default_client():
    """Returns a real AlpacaClient if both credentials are present,
    otherwise raises -- unlike market_data.get_default_client(), there
    is no silent mock fallback here. A live-trading task with no real
    broker connection configured must fail loudly and refuse to run,
    never quietly no-op or pretend to trade. Callers that explicitly
    want a mock (offline tests) construct MockAlpacaClient directly."""
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_API_SECRET"):
        return AlpacaClient()
    raise AlpacaError(
        "No ALPACA_API_KEY/ALPACA_API_SECRET configured. Refusing to proceed -- "
        "live trading never runs without a real, explicit broker connection."
    )
