"""Arbitrum USDC -> native ETH via CoW intents; no wallet keys or gas treasury.

The user signs a bounded USDC permit and an exact-output-floor sell order.
Only CoW solvers submit transactions. Quotes are not evidence of execution.
"""
import json
import re
import time
from datetime import datetime

import httpx
from eth_abi import decode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from economic_machine.values import MachineError

from .datapass import address, call_data
from .fuel_price import price_floor
from .fuel_rpc import configured_rpcs, source_label

CHAIN = 42161
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
SETTLEMENT = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
RELAYER = "0xc92e8bdf79f0507f65a392b0ab4667716bfe0110"
RPCS = configured_rpcs()
API = "https://api.cow.fi/arbitrum_one/api/v1"
# ABI representability, not a product spending limit. Wallet balance and owner
# policy determine spendable capital; amounts remain exact integer strings.
MAX_ATOMS = (1 << 256) - 1
SLIPPAGE_BPS = 50
DOMAIN_FIELDS = [{"name": k, "type": t} for k, t in
                 [("name", "string"), ("version", "string"), ("chainId", "uint256"),
                  ("verifyingContract", "address")]]
PERMIT_FIELDS = [{"name": k, "type": t} for k, t in
                 [("owner", "address"), ("spender", "address"), ("value", "uint256"),
                  ("nonce", "uint256"), ("deadline", "uint256")]]
ORDER_FIELDS = [{"name": k, "type": t} for k, t in
                [("sellToken", "address"), ("buyToken", "address"), ("receiver", "address"),
                 ("sellAmount", "uint256"), ("buyAmount", "uint256"), ("validTo", "uint32"),
                 ("appData", "bytes32"), ("feeAmount", "uint256"), ("kind", "string"),
                 ("partiallyFillable", "bool"), ("sellTokenBalance", "string"),
                 ("buyTokenBalance", "string")]]


def typed(kind, message):
    return {"types": {"EIP712Domain": DOMAIN_FIELDS, kind: PERMIT_FIELDS if kind == "Permit" else ORDER_FIELDS},
            "primaryType": kind, "domain": {"name": "USD Coin" if kind == "Permit" else "Gnosis Protocol",
            "version": "2" if kind == "Permit" else "v2", "chainId": CHAIN,
            "verifyingContract": USDC if kind == "Permit" else SETTLEMENT}, "message": message}


def recover(data, signature, owner):
    if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
        raise MachineError("WALLET_SIGNATURE_REQUIRED")
    try:
        recovered = Account.recover_message(encode_typed_data(full_message=data), signature=signature)
    except (ValueError, TypeError) as exc:
        raise MachineError("INVALID_SIGNATURE") from exc
    if recovered.lower() != owner.lower():
        raise MachineError("SIGNATURE_OWNER_MISMATCH")


def order_uid(data, owner):
    encoded = encode_typed_data(full_message=data)
    digest = keccak(b"\x19" + encoded.version + encoded.header + encoded.body)
    return "0x" + digest.hex() + owner[2:].lower() + int(data["message"]["validTo"]).to_bytes(4, "big").hex()


def read_json(method, url, body=None):
    with httpx.Client(timeout=25, trust_env=False, follow_redirects=False) as client:
        with client.stream(method, url, json=body) as response:
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 128000:
                    raise MachineError("UPSTREAM_RESPONSE_LIMIT")
            if response.status_code == 404:
                raise MachineError("ORDER_NOT_FOUND_RECONCILE_ONLY")
            if not 200 <= response.status_code < 300:
                # Never log signed payloads or raw upstream error bodies.
                try:
                    code = json.loads(payload).get("errorType", "UPSTREAM_UNAVAILABLE")
                except (ValueError, AttributeError):
                    code = "UPSTREAM_UNAVAILABLE"
                raise MachineError("COW_" + re.sub(r"[^A-Za-z0-9_]", "", str(code))[:80])
    return json.loads(payload)


def rpc(url, method, params):
    if url not in RPCS or method not in {"eth_chainId", "eth_getBalance", "eth_call", "eth_getCode",
            "eth_getBlockByNumber", "eth_getTransactionReceipt"}:
        raise MachineError("READ_ONLY_ARBITRUM_RPC_REQUIRED")
    try:
        result = read_json("POST", url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    except httpx.HTTPError:
        # HTTP exceptions include the full URL, which may contain a paid RPC token.
        raise MachineError("RPC_READ_UNAVAILABLE") from None
    if result.get("id") != 1 or "error" in result or "result" not in result:
        raise MachineError("RPC_READ_OR_SIMULATION_FAILED")
    return result["result"]


class GasRouter:
    def __init__(self, reader=rpc, api=read_json, clock=time.time):
        self.reader, self.api, self.clock = reader, api, clock

    def balances(self, owner):
        rows = []
        for index, url in enumerate(RPCS):
            if int(self.reader(url, "eth_chainId", []), 16) != CHAIN:
                raise MachineError("WRONG_CHAIN")
            def call(token, data, url=url):
                return self.reader(url, "eth_call", [{"to": token, "data": data}, "latest"])
            nonce = int(call(USDC, call_data("nonces(address)", ["address"], [owner])), 16)
            balance = int(call(USDC, call_data("balanceOf(address)", ["address"], [owner])), 16)
            native = int(self.reader(url, "eth_getBalance", [owner, "latest"]), 16)
            probe = typed("Permit", {"owner": owner, "spender": RELAYER, "value": "1", "nonce": str(nonce), "deadline": 1})
            if call(USDC, "0x3644e515").lower() != "0x" + encode_typed_data(full_message=probe).header.hex():
                raise MachineError("USDC_PERMIT_DOMAIN_MISMATCH")
            settlement = typed("Order", {"sellToken": USDC, "buyToken": ETH, "receiver": owner,
                "sellAmount": "1", "buyAmount": "1", "validTo": 1, "appData": "0x" + "00"*32,
                "feeAmount": "0", "kind": "sell", "partiallyFillable": False,
                "sellTokenBalance": "erc20", "buyTokenBalance": "erc20"})
            if call(SETTLEMENT, call_data("domainSeparator()", [], [])).lower() != "0x" + encode_typed_data(full_message=settlement).header.hex():
                raise MachineError("SETTLEMENT_DOMAIN_MISMATCH")
            actual_relayer = decode(["address"], bytes.fromhex(call(SETTLEMENT, call_data("vaultRelayer()", [], []))[2:]))[0]
            if actual_relayer.lower() != RELAYER:
                raise MachineError("RELAYER_MISMATCH")
            rows.append({"rpc": source_label(index), "usdc_atoms": str(balance), "eth_wei": str(native), "nonce": str(nonce)})
        if len({r["nonce"] for r in rows}) != 1:
            raise MachineError("PERMIT_NONCE_DISAGREEMENT")
        return rows

    def quote(self, owner, amount, deadline, hook=None, verified=False):
        app = json.dumps({"appCode": "SKEW Gas Router", "version": "1.3.0", "metadata": {
            "orderClass": {"orderClass": "market"}, "quote": {"slippageBips": SLIPPAGE_BPS},
            "hooks": {"pre": [hook or {"target": USDC, "callData": "0x", "gasLimit": "80000"}], "post": []}}}, separators=(",", ":"))
        app_hash = "0x" + keccak(text=app).hex()
        reply = self.api("POST", API + "/quote", {"sellToken": USDC, "buyToken": ETH,
            "from": owner, "receiver": owner, "sellAmountBeforeFee": str(amount), "kind": "sell",
            "signingScheme": "eip712", "validTo": deadline, "appData": app, "appDataHash": app_hash,
            "priceQuality": "verified" if verified else "optimal"})
        q = reply["quote"]
        if (any(q.get(k, "").lower() != v.lower() for k, v in
                [("sellToken", USDC), ("buyToken", ETH), ("receiver", owner)])
            or reply.get("from", "").lower() != owner.lower()
            or q.get("kind") != "sell" or q.get("partiallyFillable") is not False
            or q.get("sellTokenBalance") != "erc20" or q.get("buyTokenBalance") != "erc20"
            or q.get("validTo") != deadline or q.get("appDataHash") != app_hash or q.get("appData") != app
            or q.get("signingScheme") != "eip712"):
            raise MachineError("QUOTE_POLICY_MISMATCH")
        for k in ("sellAmount", "buyAmount", "feeAmount"):
            if not re.fullmatch(r"[0-9]{1,78}", q.get(k, "")):
                raise MachineError("INVALID_QUOTE_AMOUNT")
        if (int(q["sellAmount"]) + int(q["feeAmount"]) != amount or int(q["buyAmount"]) <= 0
                or not 0 < int(q["sellAmount"]) <= MAX_ATOMS or int(q["feeAmount"]) > 100000):
            raise MachineError("QUOTE_COST_EXCEEDS_LIMIT")
        fee_bps = str(reply.get("protocolFeeBps", "0"))
        if not re.fullmatch(r"[0-9]{1,3}", fee_bps) or int(fee_bps) > 100:
            raise MachineError("PROTOCOL_FEE_EXCEEDS_LIMIT")
        if verified and reply.get("verified") is not True:
            raise MachineError("SIGNED_PERMIT_QUOTE_NOT_VERIFIED")
        expiry = int(datetime.fromisoformat(reply["expiration"].replace("Z", "+00:00")).timestamp())
        if expiry <= self.clock() + 10:
            raise MachineError("QUOTE_EXPIRED")
        # Sell the exact total, including fees, following the current CoW SDK flow.
        # The chain enforces this ETH floor even if an upstream estimate is wrong.
        minimum = int(q["buyAmount"]) * (10000-int(fee_bps)) // 10000 * (10000-SLIPPAGE_BPS) // 10000
        anchor = price_floor(self.reader, RPCS, self.clock, amount, minimum)
        if expiry <= self.clock() + 10:
            raise MachineError("QUOTE_EXPIRED_DURING_PRICE_CHECK")
        return {"reply": reply, "app": app, "app_hash": app_hash, "minimum": str(minimum), "expires": expiry, "price_guard": anchor}

    def prepare(self, owner, amount):
        owner = address(owner)
        if type(amount) is not int or not 1 <= amount <= MAX_ATOMS:
            raise MachineError("POSITIVE_UINT256_USDC_ATOMS_REQUIRED")
        rows = self.balances(owner)
        if min(int(row["usdc_atoms"]) for row in rows) < amount:
            raise MachineError("INSUFFICIENT_USDC")
        deadline = int(self.clock()) + 600
        q = self.quote(owner, amount, deadline)
        return {"schema": "skew-gas-swap-1", "chain_id": CHAIN, "owner": owner,
            "amount_atoms": str(amount), "valid_to": deadline, "created_at": int(self.clock()),
            "permit": typed("Permit", {"owner": owner, "spender": RELAYER, "value": str(amount),
                "nonce": rows[0]["nonce"], "deadline": deadline}), "observations": rows,
            "preview_buy_wei": q["reply"]["quote"]["buyAmount"], "preview_fee_atoms": q["reply"]["quote"]["feeAmount"],
            "preview_minimum_wei": q["minimum"], "preview_verified": False, "status": "PERMIT_REQUIRED",
            "price_guard": q["price_guard"],
            "broadcasts": 0, "private_key_required": False}

    def order(self, intent, signature):
        if intent["valid_to"] <= self.clock() + 90:
            raise MachineError("PERMIT_REVIEW_EXPIRED")
        recover(intent["permit"], signature, intent["owner"])
        p = intent["permit"]["message"]
        sig = bytes.fromhex(signature[2:]); v = sig[64] + (27 if sig[64] < 27 else 0)
        data = call_data("permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
            ["address", "address", "uint256", "uint256", "uint8", "bytes32", "bytes32"],
            [p["owner"], p["spender"], int(p["value"]), p["deadline"], v, sig[:32], sig[32:64]])
        # eth_call executes the permit transiently; no approval is broadcast here.
        for url in RPCS:
            self.reader(url, "eth_call", [{"to": USDC, "data": data, "from": SETTLEMENT}, "latest"])
        hook = {"target": USDC, "callData": data, "gasLimit": "80000"}
        q = self.quote(intent["owner"], int(intent["amount_atoms"]), intent["valid_to"], hook, True)
        message = {"sellToken": USDC, "buyToken": ETH, "receiver": intent["owner"],
            "sellAmount": intent["amount_atoms"], "buyAmount": q["minimum"], "validTo": intent["valid_to"],
            "appData": q["app_hash"], "feeAmount": "0", "kind": "sell", "partiallyFillable": False,
            "sellTokenBalance": "erc20", "buyTokenBalance": "erc20"}
        signed = typed("Order", message)
        return {**intent, "order": signed, "order_uid": order_uid(signed, intent["owner"]),
            "app_data": q["app"], "quote_id": q["reply"]["id"], "quote_expires": q["expires"],
            "quoted_buy_wei": q["reply"]["quote"]["buyAmount"], "minimum_buy_wei": q["minimum"],
            "estimated_fee_atoms": q["reply"]["quote"]["feeAmount"], "protocol_fee_bps": q["reply"].get("protocolFeeBps", "0"),
            "verified_quote": True, "price_guard": q["price_guard"], "status": "ORDER_SIGNATURE_REQUIRED"}

    def submission(self, intent, signature):
        if intent["quote_expires"] <= self.clock() or intent["valid_to"] <= self.clock():
            raise MachineError("QUOTE_EXPIRED")
        recover(intent["order"], signature, intent["owner"])
        return {**intent["order"]["message"], "appData": intent["app_data"],
                "appDataHash": intent["order"]["message"]["appData"], "from": intent["owner"],
                "signingScheme": "eip712", "signature": signature, "quoteId": intent["quote_id"]}

    def submit(self, body):
        return self.api("POST", API + "/orders", body)

    def status(self, intent):
        uid = intent["order_uid"]
        if not re.fullmatch(r"0x[0-9a-f]{112}", uid):
            raise MachineError("INVALID_ORDER_UID")
        try:
            order = self.api("GET", API + "/orders/" + uid)
        except MachineError as exc:
            if str(exc) != "ORDER_NOT_FOUND_RECONCILE_ONLY":
                raise
            order = {"status": "not_found"}
        # An API's fulfilled flag is not an independently verified chain receipt.
        result = {"status": "PENDING", "provider_status": order.get("status"), "order_uid": uid,
                  "chain_verified": False, "safe_to_retry": False, "tx_hashes": []}
        if order.get("status") != "fulfilled":
            if self.clock() > intent["valid_to"]:
                # Only finalized chain state past the deadline can release uncertainty.
                for url in RPCS:
                    if int(self.reader(url, "eth_chainId", []), 16) != CHAIN:
                        raise MachineError("WRONG_CHAIN")
                    final = self.reader(url, "eth_getBlockByNumber", ["finalized", False])
                    if int(final["timestamp"], 16) <= intent["valid_to"]:
                        return result
                    filled = self.reader(url, "eth_call", [{"to": SETTLEMENT,
                        "data": call_data("filledAmount(bytes)", ["bytes"], [bytes.fromhex(uid[2:])])}, final["number"]])
                    if int(filled, 16) != 0:
                        return {**result, "status": "FILLED_RECEIPT_RECONCILIATION_REQUIRED"}
                return {**result, "status": "EXPIRED_UNFILLED", "safe_to_retry": True}
            return result
        trades = self.api("GET", API + "/trades?orderUid=" + uid)
        hashes = sorted({t["txHash"] for t in trades if t.get("orderUid") == uid})
        if len(hashes) != 1 or not re.fullmatch(r"0x[0-9a-fA-F]{64}", hashes[0]):
            raise MachineError("SETTLEMENT_RECEIPT_REQUIRED")
        result["tx_hashes"] = hashes
        topic = "0x" + keccak(text="Trade(address,address,address,uint256,uint256,uint256,bytes)").hex()
        proofs, finalized_heights = [], []
        for index, url in enumerate(RPCS):
            if int(self.reader(url, "eth_chainId", []), 16) != CHAIN:
                raise MachineError("WRONG_CHAIN")
            receipt = self.reader(url, "eth_getTransactionReceipt", hashes)
            if not receipt or int(receipt["status"], 16) != 1:
                return {**result, "status": "SETTLEMENT_UNCONFIRMED"}
            block = self.reader(url, "eth_getBlockByNumber", [receipt["blockNumber"], False])
            final = self.reader(url, "eth_getBlockByNumber", ["finalized", False])
            if block["hash"] != receipt["blockHash"] or int(final["number"], 16) < int(receipt["blockNumber"], 16):
                return {**result, "status": "AWAITING_FINALITY"}
            finalized_heights.append(int(final["number"], 16))
            matching = []
            for event in receipt["logs"]:
                if event["address"].lower() != SETTLEMENT or not event["topics"] or event["topics"][0].lower() != topic:
                    continue
                values = decode(["address", "address", "uint256", "uint256", "uint256", "bytes"], bytes.fromhex(event["data"][2:]))
                if "0x" + values[5].hex() == uid:
                    matching.append((event, values))
            if len(matching) != 1:
                raise MachineError("ORDER_TRADE_EVENT_MISSING")
            event, values = matching[0]
            if (len(event["topics"]) != 2 or event["topics"][1][-40:].lower() != intent["owner"][2:].lower()
                or values[0].lower() != USDC or values[1].lower() != ETH
                or values[2] != int(intent["amount_atoms"]) or values[3] < int(intent["minimum_buy_wei"])):
                raise MachineError("SETTLEMENT_POLICY_MISMATCH")
            proofs.append({"rpc": source_label(index), "block_hash": receipt["blockHash"], "buy_wei": str(values[3]),
                           "block_number": int(receipt["blockNumber"], 16), "sell_atoms": str(values[2])})
        if any(proofs[0][key] != proofs[1][key] for key in ("block_hash", "block_number", "buy_wei")):
            raise MachineError("RPC_SETTLEMENT_DISAGREEMENT")
        # Reconcile holdings at the same newer finalized block on both RPCs.
        # This avoids requiring the old settlement-state trie indefinitely and
        # also detects funds spent since settlement. Never use the latest tip.
        balance_height = min(finalized_heights)
        before = max(int(r["eth_wei"]) for r in intent["observations"])
        for index, url in enumerate(RPCS):
            tag = hex(balance_height)
            block = self.reader(url, "eth_getBlockByNumber", [tag, False])
            if int(block["number"], 16) != balance_height or balance_height < proofs[index]["block_number"]:
                raise MachineError("FINALIZED_BALANCE_BLOCK_REQUIRED")
            after = int(self.reader(url, "eth_getBalance", [intent["owner"], tag]), 16)
            proofs[index].update({"balance_block_number": balance_height, "balance_block_hash": block["hash"],
                                  "eth_balance_wei": str(after)})
        if any(proofs[0][key] != proofs[1][key] for key in ("balance_block_hash", "eth_balance_wei")):
            raise MachineError("RPC_BALANCE_DISAGREEMENT")
        if int(proofs[0]["eth_balance_wei"]) < before + int(intent["minimum_buy_wei"]):
            return {**result, "status": "BALANCE_RECONCILIATION_REQUIRED"}
        return {**result, "status": "FILLED_FINALIZED", "chain_verified": True, "proofs": proofs}
