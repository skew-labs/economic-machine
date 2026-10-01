"""x402 v2 HTTP admission boundary. No signatures or facilitator calls.

Real-token bindings must come from an approved agreement/registry, never from
an untrusted 402 response. TEST_CREDIT is deliberately unsupported here.
"""

import base64
import binascii
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from economic_machine.values import MachineError, digest


def decode_header(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 16384:
        raise MachineError("invalid x402 header size")

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise MachineError("duplicate x402 JSON key")
            result[key] = item
        return result

    def invalid_constant(_value):
        raise MachineError("invalid JSON constant")

    try:
        body = json.loads(base64.b64decode(value, validate=True), object_pairs_hook=unique,
                          parse_constant=invalid_constant)
    except (binascii.Error, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MachineError("malformed x402 header") from exc
    if not isinstance(body, dict):
        raise MachineError("x402 header must contain an object")
    return body


def address(value):
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
        raise MachineError("invalid EVM payment address")
    return value.lower()


@dataclass(frozen=True)
class PaymentBinding:
    terms_hash: str
    resource_url: str
    network: str
    asset: str
    pay_to: str
    amount_atoms: int
    expires: int
    max_timeout_seconds: int = 60
    token_name: str = "USDC"
    token_version: str = "2"

    def validate(self):
        if not isinstance(self.terms_hash, str) or re.fullmatch(r"[0-9a-f]{64}", self.terms_hash) is None:
            raise MachineError("approved agreement hash required")
        if not isinstance(self.network, str) or re.fullmatch(r"eip155:[1-9][0-9]{0,31}", self.network) is None:
            raise MachineError("approved EVM network required")
        if not isinstance(self.resource_url, str):
            raise MachineError("approved HTTPS resource required")
        parsed = urlsplit(self.resource_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or len(self.resource_url) > 2048):
            raise MachineError("approved HTTPS resource required")
        address(self.asset)
        address(self.pay_to)
        if (not isinstance(self.token_name, str) or not 1 <= len(self.token_name) <= 80
                or not isinstance(self.token_version, str) or not 1 <= len(self.token_version) <= 20):
            raise MachineError("approved EIP-712 token domain required")
        if (type(self.amount_atoms) is not int or not 0 < self.amount_atoms < 2 ** 256
                or type(self.expires) is not int or type(self.max_timeout_seconds) is not int
                or not 1 <= self.max_timeout_seconds <= 3600):
            raise MachineError("invalid approved payment amount or lifetime")


def admit_required(header, binding, current_terms_hash, now):
    binding.validate()
    if current_terms_hash != binding.terms_hash or binding.expires <= now:
        raise MachineError("expired or changed agreed terms")
    body = decode_header(header)
    if type(body.get("x402Version")) is not int or body["x402Version"] != 2:
        raise MachineError("only x402 version 2 is admitted")
    resource = body.get("resource")
    if not isinstance(resource, dict) or resource.get("url") != binding.resource_url:
        raise MachineError("payment resource differs from agreement")
    accepts = body.get("accepts")
    if not isinstance(accepts, list) or not 1 <= len(accepts) <= 16:
        raise MachineError("bounded payment requirements required")
    compatible = []
    for requirement in accepts:
        if not isinstance(requirement, dict):
            raise MachineError("invalid payment requirement")
        extra = requirement.get("extra", {})
        if not isinstance(extra, dict):
            raise MachineError("invalid payment mechanism metadata")
        if (requirement.get("scheme") != "exact" or requirement.get("network") != binding.network
                or requirement.get("amount") != str(binding.amount_atoms)):
            continue
        if address(requirement.get("asset")) != address(binding.asset):
            continue
        if address(requirement.get("payTo")) != address(binding.pay_to):
            continue
        timeout = requirement.get("maxTimeoutSeconds")
        if type(timeout) is not int or not 1 <= timeout <= binding.max_timeout_seconds:
            continue
        # This adapter only admits the default exact EVM EIP-3009 authorization flow.
        if (extra.get("assetTransferMethod", "eip3009") != "eip3009"
                or extra.get("paymentFlow", "authorization") != "authorization"
                or extra.get("name") != binding.token_name or extra.get("version") != binding.token_version):
            continue
        compatible.append(requirement)
    if len(compatible) != 1:
        raise MachineError("one unambiguous payment option matching agreement required")
    return {"status": "SIGNATURE_REQUIRED", "x402Version": 2, "accepted": compatible[0],
            "terms_hash": binding.terms_hash, "challenge_hash": digest(body),
            "authorization_expires": min(binding.expires, now + compatible[0]["maxTimeoutSeconds"]),
            "payment_status": "UNPAID", "tx_hash": None}


def inspect_response(header, binding):
    """A server's success claim never substitutes for independent chain readback."""
    binding.validate()
    body = decode_header(header)
    if type(body.get("success")) is not bool or body.get("network") != binding.network:
        raise MachineError("invalid settlement response network or status")
    tx = body.get("transaction")
    if not isinstance(tx, str) or (tx and re.fullmatch(r"0x[0-9a-fA-F]{64}", tx) is None):
        raise MachineError("invalid settlement transaction")
    if body["success"] and not tx:
        raise MachineError("settlement claim requires a transaction hash")
    if body.get("errorReason") == "settlement_pending" and not tx:
        raise MachineError("pending settlement requires a transaction hash")
    if "amount" in body and body["amount"] != str(binding.amount_atoms):
        raise MachineError("settled amount differs from agreement")
    return {"payment_status": "SETTLEMENT_REPORTED" if tx else "NO_BROADCAST_REPORTED",
            "tx_hash": tx or None, "server_claimed_success": body["success"],
            "chain_reconciliation": "REQUIRED", "safe_to_retry_payment": False,
            "terms_hash": binding.terms_hash}
