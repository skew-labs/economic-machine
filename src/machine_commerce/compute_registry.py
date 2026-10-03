"""Keyless compute-provider connection checks. Never signs or starts work.

An unsigned 402 proves payment requirements were observed, not that a GPU,
inference response, lease, capacity or seller service-level promise exists.
Providers stay outside the payable catalog until their adapter is admitted.
"""

import base64
import hashlib
import threading

from economic_machine.values import MachineError, digest

from .transport import HTTPS
from .x402 import address, decode_header

URL = "https://gate402.app/v1/infer"
REQUEST = {"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "Return OK."}], "max_tokens": 8}
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
NETWORK = "eip155:42161"


class ComputeRegistry:
    def __init__(self, store, transport=None, *, admitted=False):
        self.store, self.transport = store, transport or HTTPS({URL})
        self.admitted = admitted
        self.lock = threading.Lock()
        self.cache = None

    def catalog(self):
        with self.lock:
            return {"providers": [self.cache or self.initial()], "gpu_lease_required": True}

    def initial(self):
        return {"id": "gate402-inference", "name": "Gate402", "kind": "INFERENCE_API", "url": URL,
            "network": NETWORK, "credential": "CUSTOMER_WALLET_X402", "api_key_required": False,
            "status": "NOT_CHECKED", "payment_adapter_status": "ADMITTED" if self.admitted else "NOT_ADMITTED",
            "purchase_enabled": self.admitted, "capacity_verified": False, "workloads_started": 0,
            "resource_id": "gate402-inference" if self.admitted else None}

    def probe(self, provider_id):
        if provider_id != "gate402-inference":
            raise MachineError("registered compute provider required")
        # One bounded unsigned probe per two minutes, shared across callers.
        with self.lock:
            now = self.store.clock()
            if self.cache and now - self.cache["checked_at"] < 120:
                return self.cache
            result = self.initial() | {"checked_at": now, "request_hash": digest(REQUEST),
                "evidence_scope": "UNSIGNED_PAYMENT_REQUIREMENTS_ONLY", "signatures_sent": 0}
            try:
                status, headers, _ = self.transport.call(URL, REQUEST, {"x-payment-network": NETWORK})
                if status != 402:
                    raise MachineError("provider did not return an unpaid payment challenge")
                body = decode_header(headers.get("payment-required"))
                accepts = body.get("accepts")
                if (body.get("x402Version") != 2 or body.get("resource", {}).get("url") != URL
                        or not isinstance(accepts, list) or not 1 <= len(accepts) <= 16):
                    raise MachineError("bounded x402 v2 provider challenge required")
                options = []
                for item in accepts:
                    if not isinstance(item, dict):
                        raise MachineError("invalid provider requirement")
                    if item.get("network") != NETWORK or item.get("scheme") != "exact":
                        continue
                    extra = item.get("extra", {})
                    if (address(item.get("asset")) != USDC or extra.get("name") != "USD Coin"
                            or extra.get("version") != "2"):
                        raise MachineError("provider token or EIP-712 domain differs from Circle USDC")
                    recipient, amount = address(item.get("payTo")), item.get("amount")
                    timeout = item.get("maxTimeoutSeconds")
                    if (recipient == "0x" + "0" * 40 or not isinstance(amount, str) or not 1 <= len(amount) <= 8 or not amount.isascii()
                            or not amount.isdigit() or str(int(amount)) != amount or not 0 < int(amount) <= 10_000_000
                            or type(timeout) is not int or not 1 <= timeout <= 3600):
                        raise MachineError("bounded provider payment amount, recipient and lifetime required")
                    options.append({"asset": USDC, "pay_to": recipient, "amount_atoms": amount,
                        "max_timeout_seconds": timeout})
                if len(options) != 1:
                    raise MachineError("one unambiguous Arbitrum USDC provider quote required")
                # Provider discovery extensions may contain floating-point
                # examples. Hash the validated raw JSON bytes; only the exact
                # integer payment requirement enters the numeric engine.
                result.update(status="QUOTE_OBSERVED", quote=options[0],
                    challenge_hash=hashlib.sha256(base64.b64decode(headers["payment-required"], validate=True)).hexdigest(),
                    challenge_hash_scheme="SHA256_RAW_X402_JSON_BYTES")
            except Exception:  # noqa: BLE001 - arbitrary provider and transport data stay private.
                # Do not expose headers, arbitrary response bodies or network internals.
                result.update(status="UNAVAILABLE", reason="PROVIDER_QUOTE_NOT_VERIFIED")
            self.cache = result
            return result
