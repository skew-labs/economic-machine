"""Read-only connectors. Keys are resolved from the user's process environment.

Only public, pinned HTTPS egress is used. No wallet signing, order placement,
withdrawal, model inference, marketplace payment or secret persistence exists here.
"""

import hashlib
import hmac
import json
import os
import re
import time
from decimal import Decimal, localcontext
from urllib.parse import urlencode

import httpx

from economic_machine.values import MachineError, decstr, digest, require_keys
from machine_commerce.transport import PinnedTransport, https_url, integer
from machine_commerce.x402 import address

PROFILES = {
    "arbitrum-sepolia-wallet": {"kind": "wallet", "name": "Arbitrum Sepolia", "credentials": [],
        "fields": ["address"], "operations": ["READ_BALANCES"], "network": "eip155:421614"},
    "binance-spot": {"kind": "exchange", "name": "Binance Spot", "credentials": ["api_key_env", "api_secret_env"],
        "fields": [], "operations": ["READ_BALANCES", "READ_OPEN_ORDERS"], "network": "binance-spot"},
    "binance-usdm": {"kind": "exchange", "name": "Binance USD-M", "credentials": ["api_key_env", "api_secret_env"],
        "fields": [], "operations": ["READ_BALANCES", "READ_OPEN_ORDERS", "READ_DERIVATIVE_POSITIONS"], "network": "binance-usdm"},
    "json-data": {"kind": "data", "name": "JSON data", "credentials": ["api_key_env"],
        "fields": ["url"], "operations": ["READ_DATA"], "network": "https"},
    "openai-compatible": {"kind": "ai", "name": "AI API", "credentials": ["api_key_env"],
        "fields": ["url"], "operations": ["LIST_MODELS"], "network": "https"},
}
RPC = "https://sepolia-rollup.arbitrum.io/rpc"
USDC = "0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d"


def normalize_connection(raw):
    require_keys(raw, {"name", "profile", "config"}, "connection")
    if (not isinstance(raw["profile"], str) or raw["profile"] not in PROFILES
            or not isinstance(raw["name"], str) or not 1 <= len(raw["name"]) <= 60):
        raise MachineError("supported connection and bounded name required")
    if any(ord(c) < 32 for c in raw["name"]):
        raise MachineError("printable connection name required")
    profile, config = PROFILES[raw["profile"]], raw["config"]
    require_keys(config, set(profile["fields"] + profile["credentials"]), "connection references")
    for field in profile["credentials"]:
        if not isinstance(config[field], str) or re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", config[field]) is None:
            raise MachineError("use an environment variable name, not a secret")
    if "address" in config:
        config = config | {"address": address(config["address"])}
    if "url" in config:
        https_url(config["url"])
        if raw["profile"] == "openai-compatible" and not config["url"].endswith("/models"):
            raise MachineError("AI connector needs the read-only models endpoint")
    return {"name": raw["name"], "profile": raw["profile"], "kind": profile["kind"],
            "network": profile["network"], "config": dict(config)}


def credential(env_name):
    value = os.environ.get(env_name)
    if not value or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise MachineError("CREDENTIAL_REFERENCE_UNAVAILABLE")
    return value


class ReadOnlyHTTP:
    def request(self, url, *, method="GET", body=None, headers=None, params=None):
        https_url(url)
        if method not in {"GET", "POST"}:
            raise MachineError("READ_ONLY_METHOD_REQUIRED")
        # A query-aware delegate pins DNS/TLS before it sends the original query.
        delegate = httpx.HTTPTransport(retries=0, trust_env=False)
        class RestoreQuery:
            def handle_request(self, request):
                request.url = request.url.copy_with(query=encoded_query)
                return delegate.handle_request(request)
            def close(self):
                delegate.close()

        encoded_query = urlencode(params or {}).encode()
        transport = PinnedTransport([url], delegate=RestoreQuery())
        with httpx.Client(transport=transport, timeout=httpx.Timeout(10, connect=3), trust_env=False,
                          follow_redirects=False) as client, client.stream(method, url, json=body, headers=headers or {}) as response:
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 500_000:
                    raise MachineError("READ_RESPONSE_TOO_LARGE")
            if response.status_code != 200:
                raise MachineError("REMOTE_READ_REJECTED")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise MachineError("INVALID_REMOTE_JSON") from exc
        return result


class Connectors:
    def __init__(self, http=None, clock=time.time):
        self.http, self.clock = http or ReadOnlyHTTP(), clock

    def read(self, connection):
        profile = connection["profile"]
        if profile == "arbitrum-sepolia-wallet":
            result = self.wallet(connection)
        elif profile == "binance-spot":
            result = self.binance(connection)
        elif profile == "binance-usdm":
            result = self.derivatives(connection)
        elif profile == "openai-compatible":
            result = self.models(connection)
        elif profile == "json-data":
            result = self.data(connection)
        else:
            raise MachineError("UNSUPPORTED_CONNECTION")
        serialized = json.dumps(result)
        for name in PROFILES[profile]["credentials"]:
            if credential(connection["config"][name]) in serialized:
                raise MachineError("CREDENTIAL_ECHO_BLOCKED")
        return result | {"observed_at": int(self.clock()), "source_hash": digest(result), "read_only": True}

    def rpc(self, method, params):
        result = self.http.request(RPC, method="POST", body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(result, dict) or result.get("id") != 1 or "error" in result or "result" not in result:
            raise MachineError("CHAIN_READ_UNAVAILABLE")
        return result["result"]

    def wallet(self, connection):
        owner = connection["config"]["address"]
        if integer(self.rpc("eth_chainId", [])) != 421614:
            raise MachineError("WRONG_CHAIN")
        block = self.rpc("eth_getBlockByNumber", ["finalized", False])
        height = block["number"]
        integer(height)
        eth = integer(self.rpc("eth_getBalance", [owner, height]))
        call = "0x70a08231" + owner[2:].rjust(64, "0")
        usdc = integer(self.rpc("eth_call", [{"to": USDC, "data": call}, height]))
        with localcontext() as context:
            context.prec = 96
            assets = [{"symbol": "ETH", "quantity": decstr(Decimal(eth) / Decimal(10 ** 18)), "decimals": 18},
                      {"symbol": "USDC", "quantity": decstr(Decimal(usdc) / Decimal(10 ** 6)), "decimals": 6}]
        return {"assets": assets,
                "positions": [], "orders": [], "address": owner, "block_number": integer(height),
                "assurance": "RPC_FINALIZED_TAG", "network": "eip155:421614"}

    def binance(self, connection):
        config = connection["config"]
        key, secret = credential(config["api_key_env"]), credential(config["api_secret_env"])
        def get(path):
            params = {"timestamp": int(self.clock() * 1000), "recvWindow": 5000}
            encoded = urlencode(params)
            params["signature"] = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
            return self.http.request("https://api.binance.com/api/v3/" + path,
                headers={"X-MBX-APIKEY": key}, params=params)
        account, orders = get("account"), get("openOrders")
        if not isinstance(account.get("balances"), list) or not isinstance(orders, list) or len(orders) > 1000:
            raise MachineError("INVALID_EXCHANGE_SNAPSHOT")
        balances = []
        for item in account["balances"]:
            if any(not isinstance(item[k], str) or re.fullmatch(r"[0-9]{1,30}(\.[0-9]{1,18})?", item[k]) is None
                   for k in ["free", "locked"]):
                raise MachineError("INVALID_EXCHANGE_BALANCE")
            with localcontext() as context:
                context.prec = 96
                free, locked = Decimal(item["free"]), Decimal(item["locked"])
                if not free.is_finite() or not locked.is_finite() or min(free, locked) < 0:
                    raise MachineError("INVALID_EXCHANGE_BALANCE")
                if free + locked:
                    balances.append({"symbol": str(item["asset"])[:20], "quantity": decstr(free + locked),
                                     "available": decstr(free), "locked": decstr(locked)})
        public_orders = [{"id": str(o["orderId"]), "symbol": o["symbol"], "side": o["side"],
            "status": o["status"], "price": o["price"], "quantity": o["origQty"], "filled": o["executedQty"]} for o in orders]
        return {"assets": balances, "positions": [], "orders": public_orders,
                "assurance": "AUTHENTICATED_READ_ONLY_API", "network": "binance-spot"}

    def models(self, connection):
        config = connection["config"]
        response = self.http.request(config["url"], headers={"Authorization": "Bearer " + credential(config["api_key_env"])})
        models = response.get("data") if isinstance(response, dict) else None
        if not isinstance(models, list) or len(models) > 2000:
            raise MachineError("INVALID_MODEL_CATALOG")
        return {"models": [str(m["id"])[:160] for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)],
                "inference_calls": 0, "assurance": "CATALOG_READ_NOT_BILLING_READ"}

    def derivatives(self, connection):
        from economic_machine.values import decimal

        from .broker import BinanceBroker
        broker = BinanceBroker(connection, http=self.http, clock=self.clock)
        account = broker.signed("/fapi/v3/account")
        positions = broker.signed("/fapi/v3/positionRisk")
        orders = broker.signed("/fapi/v1/openOrders")
        if (not isinstance(account.get("assets"), list) or not isinstance(positions, list)
                or not isinstance(orders, list) or max(len(positions), len(orders), len(account["assets"])) > 1000):
            raise MachineError("INVALID_EXCHANGE_SNAPSHOT")
        assets = [{"symbol": a["asset"], "quantity": decstr(decimal(a["walletBalance"], signed=True)),
                   "available": decstr(decimal(a["availableBalance"], signed=True)),
                   "unrealized_pnl": decstr(decimal(a["unrealizedProfit"], signed=True))} for a in account["assets"]]
        public_positions = []
        for p in positions:
            quantity = decimal(p["positionAmt"], signed=True)
            if quantity:
                public_positions.append({"symbol": p["symbol"], "quantity": decstr(quantity), "side": p["positionSide"],
                    "entry_price": decstr(decimal(p["entryPrice"])), "mark_price": decstr(decimal(p["markPrice"])),
                    "liquidation_price": decstr(decimal(p["liquidationPrice"])),
                    "unrealized_pnl": decstr(decimal(p["unRealizedProfit"], signed=True)),
                    "notional": decstr(decimal(p["notional"], signed=True)), "margin_asset": p["marginAsset"],
                    "maintenance_margin": decstr(decimal(p["maintMargin"])), "venue_updated_at_ms": p["updateTime"]})
        public_orders = [{"id": str(o["orderId"]), "symbol": o["symbol"], "side": o["side"],
            "status": o["status"], "price": decstr(decimal(o["price"])), "quantity": decstr(decimal(o["origQty"])),
            "filled": decstr(decimal(o["executedQty"])), "reduce_only": o["reduceOnly"], "position_side": o["positionSide"]} for o in orders]
        return {"assets": assets, "positions": public_positions, "orders": public_orders,
                "assurance": "AUTHENTICATED_SEQUENTIAL_READS_NOT_ATOMIC_SNAPSHOT", "network": "binance-usdm"}

    def data(self, connection):
        config = connection["config"]
        response = self.http.request(config["url"], headers={"Authorization": "Bearer " + credential(config["api_key_env"])})
        # Never persist arbitrary vendor payloads: they may echo credentials or
        # personal data. Only content fingerprints and sizes reach the console.
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return {"content_hash": hashlib.sha256(encoded).hexdigest(), "encoded_bytes": len(encoded),
                "assurance": "HTTP_CONTENT_FINGERPRINT_NOT_DATA_QUALITY_CERTIFICATION"}
