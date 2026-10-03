"""Bounded venue ports. No credential persistence, withdrawals or HTTP retries."""

import hashlib
import hmac
import json
import re
import time
from decimal import localcontext
from typing import Protocol
from urllib.parse import urlencode

import httpx

from economic_machine.values import MachineError, decimal, decstr
from machine_commerce.transport import PinnedTransport, https_url

from .connections import credential

VENUES = {
    "binance-spot": ("https://api.binance.com", False),
    "binance-usdm": ("https://fapi.binance.com", True),
    "binance-spot-testnet": ("https://testnet.binance.vision", False),
    "binance-usdm-testnet": ("https://demo-fapi.binance.com", True),
}


class VenueRejected(MachineError):
    pass


class VenueUnavailable(MachineError):
    def __init__(self, retry_after=0):
        super().__init__("VENUE_OUTCOME_UNKNOWN")
        self.retry_after = retry_after


class VenuePort(Protocol):
    """An adapter must supply all these operations before financial admission."""

    def instrument(self, ticker): ...
    def validate(self, instruction, instrument): ...
    def guard_account(self, order, instrument): ...
    def account_snapshot(self, connection): ...
    def submit(self, plan): ...
    def query(self, plan): ...
    def cancel(self, plan): ...


class VenueHTTP:
    def request(self, url, *, method="GET", params=None, headers=None):
        https_url(url)
        if method not in {"GET", "POST", "DELETE"}:
            raise MachineError("UNSUPPORTED_VENUE_METHOD")
        query = urlencode(params or {}).encode()
        # USD-M exchangeInfo is a full instrument catalogue, even with a symbol
        # query. Only this fixed public GET gets a larger, still bounded limit.
        maximum = (
            4_000_000
            if method == "GET" and url in {base + "/fapi/v1/exchangeInfo" for base, futures in VENUES.values() if futures}
            else 500_000
        )
        delegate = httpx.HTTPTransport(retries=0, trust_env=False)

        class RestoreQuery:
            def handle_request(self, request):
                request.url = request.url.copy_with(query=query)
                return delegate.handle_request(request)

            def close(self):
                delegate.close()

        transport = PinnedTransport([url], delegate=RestoreQuery())
        with (
            httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(10, connect=3),
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream(method, url, headers=headers or {}) as response,
        ):
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > maximum:
                    raise MachineError("VENUE_RESPONSE_TOO_LARGE")
            try:
                result = json.loads(content)
            except (ValueError, UnicodeError) as exc:
                raise MachineError("VENUE_RESPONSE_UNVERIFIABLE") from exc
            if response.status_code != 200:
                # Only explicit matching-engine rejections establish no order.
                # Timeouts, WAF, rate limits and 5xx remain ambiguous.
                code = result.get("code") if isinstance(result, dict) else None
                if (
                    response.status_code == 400
                    and type(code) is int
                    and code in {-1013, -1100, -1102, -1111, -1116, -1121, -2022}
                ):
                    raise VenueRejected("VENUE_ORDER_REJECTED")
                retry_after = (
                    3600 if response.status_code == 418 else 60 if response.status_code == 429 else 0
                )
                header = response.headers.get("Retry-After", "")
                if header.isascii() and header.isdigit() and len(header) <= 6:
                    retry_after = min(86400, max(retry_after, int(header)))
                raise VenueUnavailable(retry_after)
        return result


def symbol(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9]{3,24}", value) is None:
        raise MachineError("EXACT_VENUE_SYMBOL_REQUIRED")
    return value


class BinanceBroker:
    """Spot LIMIT orders and USD-M one-way reduce-only LIMIT orders."""

    def __init__(self, connection, *, http=None, clock=time.time):
        self.connection, self.http, self.clock = connection, http or VenueHTTP(), clock
        if connection["profile"] not in VENUES:
            raise MachineError("EXECUTION_ADAPTER_UNAVAILABLE")
        self.base, self.futures = VENUES[connection["profile"]]
        self.prefix = "/fapi/v1/" if self.futures else "/api/v3/"

    def signed(self, path, *, method="GET", params=None):
        config = self.connection["config"]
        key, secret = credential(config["api_key_env"]), credential(config["api_secret_env"])
        args = dict(params or {}) | {"timestamp": int(self.clock() * 1000), "recvWindow": 5000}
        args["signature"] = hmac.new(secret.encode(), urlencode(args).encode(), hashlib.sha256).hexdigest()
        result = self.http.request(
            self.base + path, method=method, params=args, headers={"X-MBX-APIKEY": key}
        )
        encoded = json.dumps(result)
        if key in encoded or secret in encoded:
            raise MachineError("CREDENTIAL_ECHO_BLOCKED")
        return result

    def instrument(self, ticker):
        ticker = symbol(ticker)
        params = {} if self.futures else {"symbol": ticker}
        result = self.http.request(self.base + self.prefix + "exchangeInfo", params=params)
        candidates = [s for s in result.get("symbols", []) if s.get("symbol") == ticker]
        if len(candidates) != 1 or candidates[0].get("status") != "TRADING":
            raise MachineError("TRADABLE_INSTRUMENT_REQUIRED")
        item = candidates[0]
        if item.get("quoteAsset") != "USDT" or (self.futures and item.get("contractType") != "PERPETUAL"):
            raise MachineError("USDT_SPOT_OR_PERPETUAL_REQUIRED")
        filters = {f["filterType"]: f for f in item["filters"]}
        for name in ["PRICE_FILTER", "LOT_SIZE"]:
            if name not in filters:
                raise MachineError("INSTRUMENT_FILTERS_REQUIRED")
        return {
            "symbol": ticker,
            "base_asset": symbol(item["baseAsset"]),
            "quote_asset": "USDT",
            "filters": filters,
        }

    def validate(self, instruction, instrument):
        qty, price = decimal(instruction["quantity"]), decimal(instruction["price"])
        if qty <= 0 or price <= 0:
            raise MachineError("POSITIVE_ORDER_REQUIRED")
        with localcontext() as context:
            context.prec = 180
            for value, rule, step, lower, upper in [
                (qty, instrument["filters"]["LOT_SIZE"], "stepSize", "minQty", "maxQty"),
                (price, instrument["filters"]["PRICE_FILTER"], "tickSize", "minPrice", "maxPrice"),
            ]:
                increment, minimum, maximum = decimal(rule[step]), decimal(rule[lower]), decimal(rule[upper])
                if (
                    (increment and value % increment)
                    or (minimum and value < minimum)
                    or (maximum and value > maximum)
                ):
                    raise MachineError("INSTRUMENT_INCREMENT_OR_RANGE_VIOLATION")
            amount = qty * price
            for name in ["MIN_NOTIONAL", "NOTIONAL"]:
                rule = instrument["filters"].get(name)
                if rule:
                    minimum = decimal(rule.get("minNotional", rule.get("notional", "0")))
                    maximum = decimal(rule.get("maxNotional", "0"))
                    if amount < minimum or (maximum and amount > maximum):
                        raise MachineError("INSTRUMENT_NOTIONAL_VIOLATION")
            return decstr(amount)

    def guard_account(self, order, instrument):
        """Re-read actual venue balances immediately before transmission."""
        qty, price = decimal(order["quantity"]), decimal(order["price"])
        with localcontext() as context:
            context.prec = 180
            if self.futures:
                mode = self.signed("/fapi/v1/positionSide/dual")
                if mode.get("dualSidePosition") is not False or order["reduce_only"] is not True:
                    raise MachineError("ONE_WAY_REDUCE_ONLY_REQUIRED")
                positions = self.signed("/fapi/v3/positionRisk", params={"symbol": order["symbol"]})
                values = [
                    p for p in positions if p["symbol"] == order["symbol"] and p["positionSide"] == "BOTH"
                ]
                if len(values) != 1:
                    raise MachineError("UNAMBIGUOUS_POSITION_REQUIRED")
                amount = decimal(values[0]["positionAmt"], signed=True)
                if qty > abs(amount) or not (
                    (amount > 0 and order["side"] == "SELL") or (amount < 0 and order["side"] == "BUY")
                ):
                    raise MachineError("POSITION_REDUCTION_REQUIRED")
                return
            account = self.signed("/api/v3/account")
            if account.get("canTrade") is not True:
                raise MachineError("VENUE_TRADE_PERMISSION_REQUIRED")
            balances = {a["asset"]: decimal(a["free"]) for a in account["balances"]}
            required = (
                qty * price * (1 + decimal(order["fee_reserve_bps"]) / 10000)
                if order["side"] == "BUY"
                else qty
            )
            asset = instrument["quote_asset"] if order["side"] == "BUY" else instrument["base_asset"]
            if balances.get(asset, decimal("0")) < required:
                raise MachineError("INSUFFICIENT_VENUE_BALANCE")

    def submit(self, plan):
        params = {
            "symbol": plan["symbol"],
            "side": plan["side"],
            "type": "LIMIT",
            "timeInForce": plan["time_in_force"],
            "quantity": plan["quantity"],
            "price": plan["price"],
            "newClientOrderId": plan["client_order_id"],
            "newOrderRespType": "RESULT",
        }
        if self.futures:
            params |= {"reduceOnly": "true", "positionSide": "BOTH"}
        return self.signed(self.prefix + "order", method="POST", params=params)

    def account_snapshot(self, connection):
        from .connections import Connectors
        return Connectors(self.http, clock=self.clock).read(connection)

    def query(self, plan):
        return self.signed(
            self.prefix + "order",
            params={"symbol": plan["symbol"], "origClientOrderId": plan["client_order_id"]},
        )

    def cancel(self, plan):
        return self.signed(
            self.prefix + "order",
            method="DELETE",
            params={"symbol": plan["symbol"], "origClientOrderId": plan["client_order_id"]},
        )


def broker_for(connection, *, clock=time.time):
    return BinanceBroker(connection, clock=clock)
