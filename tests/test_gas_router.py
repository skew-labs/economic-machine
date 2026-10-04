import copy
import tempfile
import time
import unittest
from pathlib import Path

from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from fastapi.testclient import TestClient

from economic_machine.values import MachineError
from machine_commerce.fuel_price import ETH_USD, SEQUENCER, USDC_USD
from machine_commerce.gas_portal import SwapStore, create_gas_portal
from machine_commerce.gas_router import (
    API,
    ETH,
    RELAYER,
    RPCS,
    SETTLEMENT,
    USDC,
    GasRouter,
    call_data,
    order_uid,
    typed,
)

# Unfunded deterministic test identity; never submitted to a public order book.
TEST = Account.from_key((1).to_bytes(32, "big"))


def sign(data):
    return "0x" + Account.sign_message(encode_typed_data(full_message=data), TEST.key).signature.hex()


class FakeNetwork:
    def __init__(self):
        self.now = int(time.time())
        self.calls = []
        self.posts = 0
        self.lose_response = False
        self.quote_change = lambda value: value
        self.provider_status = "open"
        self.intent = None

    def read(self, url, method, params):
        self.calls.append((url, method, params))
        if method == "eth_chainId": return hex(42161)
        if method == "eth_getBalance": return hex(10**15 if len(params) > 1 and params[1] != "latest" else 0)
        if method == "eth_call":
            d = params[0]["data"]
            target = params[0]["to"].lower()
            if target in {ETH_USD, USDC_USD, SEQUENCER}:
                if d == call_data("description()", [], []):
                    return "0x" + encode(["string"], ["ETH / USD" if target == ETH_USD else "USDC / USD"]).hex()
                if d == call_data("decimals()", [], []): return "0x" + encode(["uint8"], [8]).hex()
                if d == call_data("latestRoundData()", [], []):
                    values = [1, 0, self.now-7200, self.now-7200, 1] if target == SEQUENCER else [1, 2700*10**8 if target == ETH_USD else 10**8, self.now-20, self.now-10, 1]
                    return "0x" + encode(["uint80", "int256", "uint256", "uint256", "uint80"], values).hex()
            if d.startswith("0x7ecebe00"): return hex(0)
            if d.startswith("0x70a08231"): return hex(27_000_000)
            if d == "0x3644e515":
                return "0x" + encode_typed_data(full_message=typed("Permit", {
                    "owner": TEST.address, "spender": RELAYER, "value": "1", "nonce": "0", "deadline": 1})).header.hex()
            if d == call_data("domainSeparator()", [], []):
                return "0x" + encode_typed_data(full_message=typed("Order", {
                    "sellToken": USDC, "buyToken": ETH, "receiver": TEST.address, "sellAmount": "1", "buyAmount": "1",
                    "validTo": 1, "appData": "0x" + "00"*32, "feeAmount": "0", "kind": "sell",
                    "partiallyFillable": False, "sellTokenBalance": "erc20", "buyTokenBalance": "erc20"})).header.hex()
            if d == call_data("vaultRelayer()", [], []): return "0x" + encode(["address"], [RELAYER]).hex()
            if d.startswith("0xd505accf"): return "0x"
            if d.startswith(call_data("filledAmount(bytes)", ["bytes"], [b""])[:10]): return hex(0)
        if method == "eth_getTransactionReceipt":
            p = self.intent
            return {"status": "0x1", "blockNumber": "0x10", "blockHash": "0x"+"ab"*32, "logs": [{
                "address": SETTLEMENT, "topics": ["0x"+keccak(text="Trade(address,address,address,uint256,uint256,uint256,bytes)").hex(), "0x"+TEST.address[2:].lower().zfill(64)],
                "data": "0x"+encode(["address", "address", "uint256", "uint256", "uint256", "bytes"],
                    [USDC, ETH, 2000000, 740000000000000, 10000, bytes.fromhex(p["order_uid"][2:])]).hex()}]}
        if method == "eth_getBlockByNumber": return {"number": "0x10", "hash": "0x"+"ab"*32, "timestamp": hex(self.now)}
        raise AssertionError((method, params))

    def api(self, method, url, body=None):
        self.calls.append((url, method, body))
        if url == API + "/quote":
            q = {"sellToken": USDC, "buyToken": ETH, "receiver": body["receiver"], "sellAmount": str(int(body["sellAmountBeforeFee"])-10000),
                 "buyAmount": "740000000000000", "feeAmount": "10000", "validTo": body["validTo"],
                 "appData": body["appData"], "appDataHash": body["appDataHash"], "kind": "sell", "partiallyFillable": False,
                 "sellTokenBalance": "erc20", "buyTokenBalance": "erc20", "signingScheme": "eip712"}
            from datetime import datetime, timezone
            return self.quote_change({"quote": q, "from": body["from"], "verified": body["priceQuality"] == "verified",
                "id": 12, "expiration": datetime.fromtimestamp(self.now+60, timezone.utc).isoformat(), "protocolFeeBps": "2"})
        if url == API + "/orders" and method == "POST":
            self.posts += 1
            if self.lose_response: raise TimeoutError("ambiguous submission")
            return self.intent["order_uid"]
        if "/orders/" in url: return {"status": self.provider_status}
        if "/trades?" in url: return [{"orderUid": self.intent["order_uid"], "txHash": "0x"+"12"*32}]
        raise AssertionError(url)


class GasRouterTests(unittest.TestCase):
    def setUp(self):
        self.net = FakeNetwork()
        self.router = GasRouter(self.net.read, self.net.api, lambda: self.net.now)

    def intent(self):
        p = self.router.prepare(TEST.address, 2000000)
        return self.router.order(p, sign(p["permit"]))

    def test_zero_eth_preview_exact_permit_native_output_and_signed_intent(self):
        p = self.intent()
        self.assertTrue(p["verified_quote"])
        self.assertEqual(p["permit"]["message"]["value"], "2000000")
        self.assertEqual(p["permit"]["message"]["spender"], RELAYER)
        self.assertEqual(p["order"]["message"]["buyToken"], ETH)
        self.assertEqual(p["order"]["message"]["feeAmount"], "0")
        self.assertEqual(p["order"]["message"]["receiver"], TEST.address)
        self.assertEqual(len(p["order_uid"]), 114)
        self.assertEqual(p["order_uid"], order_uid(p["order"], TEST.address))
        body = self.router.submission(p, sign(p["order"]))
        self.assertEqual(body["appDataHash"], "0x"+keccak(text=body["appData"]).hex())
        self.assertEqual(self.net.posts, 0)
        self.assertFalse(any(call[1] == "eth_sendRawTransaction" for call in self.net.calls))

    def test_wrong_signer_and_expired_intent_rejected(self):
        p = self.router.prepare(TEST.address, 2000000)
        foreign = Account.create()
        signature = "0x"+Account.sign_message(encode_typed_data(full_message=p["permit"]), foreign.key).signature.hex()
        with self.assertRaisesRegex(MachineError, "SIGNATURE_OWNER"):
            self.router.order(p, signature)
        p = self.intent()
        self.net.now += 61
        with self.assertRaisesRegex(MachineError, "EXPIRED"):
            self.router.submission(p, sign(p["order"]))

    def test_amount_fee_recipient_asset_and_hook_tampering_fail_closed(self):
        for amount in (0, 999999, 3000001, True, "2000000"):
            with self.subTest(amount=amount), self.assertRaises(MachineError): self.router.prepare(TEST.address, amount)
        for key, value in [("receiver", RELAYER), ("buyToken", USDC), ("sellToken", ETH),
                           ("feeAmount", "1000001"), ("sellAmount", "2000000"), ("buyAmount", "0"),
                           ("appDataHash", "0x"+"ab"*32), ("partiallyFillable", True), ("validTo", 1)]:
            def change(reply, key=key, value=value):
                reply["quote"][key] = value
                return reply
            self.net.quote_change = change
            with self.subTest(key=key), self.assertRaises(MachineError): self.router.prepare(TEST.address, 2000000)

    def test_two_rpc_nonce_domain_and_relayer_agreement(self):
        original = self.net.read
        for selector, wrong in [("0x7ecebe00", "0x2"), ("0x3644e515", "0x"+"ab"*32),
                               (call_data("vaultRelayer()", [], []), "0x"+encode(["address"], [USDC]).hex())]:
            def changed(url, method, params, selector=selector, wrong=wrong):
                if url == RPCS[1] and method == "eth_call" and params[0]["data"].startswith(selector): return wrong
                return original(url, method, params)
            r = GasRouter(changed, self.net.api, lambda: self.net.now)
            with self.subTest(selector=selector), self.assertRaises(MachineError): r.prepare(TEST.address, 2000000)

    def test_unverified_final_quote_never_reaches_order_signature(self):
        p = self.router.prepare(TEST.address, 2000000)
        self.net.quote_change = lambda reply: {**reply, "verified": False}
        with self.assertRaisesRegex(MachineError, "NOT_VERIFIED"): self.router.order(p, sign(p["permit"]))

    def test_provider_filled_is_not_chain_confirmation(self):
        p = self.intent(); self.net.intent = p
        self.net.provider_status = "fulfilled"
        r = GasRouter(lambda url, method, params: None if method == "eth_getTransactionReceipt" else self.net.read(url, method, params), self.net.api)
        self.assertFalse(r.status(p)["chain_verified"])
        self.assertTrue(self.router.status(p)["chain_verified"])
        bad = copy.deepcopy(p); bad["minimum_buy_wei"] = "900000000000000"
        with self.assertRaisesRegex(MachineError, "SETTLEMENT_POLICY"): self.router.status(bad)

    def test_independent_price_floor_and_outage_guards(self):
        def low_quote(reply):
            reply["quote"]["buyAmount"] = "1000"
            return reply
        self.net.quote_change = low_quote
        with self.assertRaisesRegex(MachineError, "INDEPENDENT_PRICE_FLOOR"): self.router.prepare(TEST.address, 2000000)
        self.net.quote_change = lambda x: x
        original = self.net.read
        for target, values, expected in [
            (SEQUENCER, [1, 1, self.net.now-7200, self.net.now-7200, 1], "SEQUENCER"),
            (SEQUENCER, [1, 0, self.net.now-30, self.net.now-30, 1], "SEQUENCER"),
            (ETH_USD, [1, 2700*10**8, self.net.now-4000, self.net.now-4000, 1], "STALE"),
            (USDC_USD, [1, -1, self.net.now-20, self.net.now-10, 1], "INVALID"),
        ]:
            def changed(url, method, params, target=target, values=values):
                if method == "eth_call" and params[0]["to"].lower() == target and params[0]["data"] == call_data("latestRoundData()", [], []):
                    return "0x"+encode(["uint80", "int256", "uint256", "uint256", "uint80"], values).hex()
                return original(url, method, params)
            with self.subTest(expected=expected), self.assertRaisesRegex(MachineError, expected):
                GasRouter(changed, self.net.api, lambda: self.net.now).prepare(TEST.address, 2000000)

    def test_expired_order_requires_finalized_unfilled_proof_on_both_rpcs(self):
        p = self.intent(); self.net.intent = p
        self.net.now = p["valid_to"]+1
        self.assertTrue(self.router.status(p)["safe_to_retry"])
        original = self.net.read
        def not_final(url, method, params):
            if url == RPCS[1] and method == "eth_getBlockByNumber":
                return {**original(url, method, params), "timestamp": hex(p["valid_to"]-1)}
            return original(url, method, params)
        self.assertFalse(GasRouter(not_final, self.net.api, lambda: self.net.now).status(p)["safe_to_retry"])
        def filled(url, method, params):
            if url == RPCS[1] and method == "eth_call" and params[0]["data"].startswith(call_data("filledAmount(bytes)", ["bytes"], [b""])[:10]): return hex(2000000)
            return original(url, method, params)
        result = GasRouter(filled, self.net.api, lambda: self.net.now).status(p)
        self.assertEqual(result["status"], "FILLED_RECEIPT_RECONCILIATION_REQUIRED")
        self.assertFalse(result["safe_to_retry"])

    def test_persist_before_submission_and_never_resubmit_after_timeout_or_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"swaps.sqlite3"
            store = SwapStore(path, self.router)
            p = store.call("quote", {"owner": TEST.address, "amount_atoms": "2000000"})
            p = store.call("order", {"id": p["id"], "signature": sign(p["permit"])})
            self.net.intent = p; self.net.lose_response = True
            request = {"id": p["id"], "signature": sign(p["order"])}
            self.assertEqual(store.call("submit", request)["status"], "UNKNOWN_RECONCILE_ONLY")
            restarted = SwapStore(path, self.router)
            self.assertFalse(restarted.call("submit", request)["safe_to_retry"])
            self.assertEqual(self.net.posts, 1)
            with self.assertRaisesRegex(MachineError, "RECONCILED"):
                restarted.call("quote", {"owner": TEST.address, "amount_atoms": "2000000"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_http_limits_origin_exact_fields_and_static_private_path(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SwapStore(Path(folder)/"swap.sqlite3", self.router)
            with TestClient(create_gas_portal(store)) as client:
                route = "/commerce/swap-api/quote"
                headers = {"Origin": "https://machine.148-113-153-116.nip.io"}
                data = {"owner": TEST.address, "amount_atoms": "2000000"}
                self.assertEqual(client.post(route, json=data).status_code, 403)
                self.assertEqual(client.post(route, json={**data, "private_key": "forbidden"}, headers=headers).status_code, 409)
                self.assertEqual(client.post(route, content="x"*2049, headers={**headers, "Content-Type": "application/json"}).status_code, 413)
                response = client.post(route, json=data, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["private_key_required"])
                self.assertEqual(client.get("/commerce/swap-assets/runtime.sqlite3").status_code, 404)
                self.assertIn("frame-ancestors 'none'", client.get("/commerce/swap").headers["content-security-policy"])


if __name__ == "__main__": unittest.main()
