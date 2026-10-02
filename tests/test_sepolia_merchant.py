"""New public-testnet merchant boundary, without re-running unchanged suites."""

import copy
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from eth_account import Account
from eth_account.messages import encode_typed_data, hash_domain
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sepolia_merchant import (
    MAX_AMOUNT,
    RPC,
    USDC,
    Merchant,
    SepoliaChain,
    create_app,
    header,
    hexbytes,
    validate_sale,
)

from economic_machine.values import MachineError
from machine_commerce.payments import typed_authorization
from machine_commerce.transport import AUTH_USED, TRANSFER, topic_address
from machine_commerce.x402 import decode_header


class SellerChain:
    def __init__(self, sale):
        self.sale, self.broadcasts, self.signed = sale, 0, 0
        self.tx_hash = "0x" + "ab" * 32
        self.fail, self.pending, self.bad = False, False, False

    def signed_transaction(self, binding, auth, signature):
        self.signed += 1
        self.auth = auth
        return self.tx_hash, b"opaque-test-transaction"

    def broadcast(self, raw):
        self.broadcasts += 1
        if self.fail:
            raise TimeoutError("submission outcome unknown")
        return self.tx_hash

    def receipt(self, tx_hash):
        if self.pending:
            return None
        return {"status": 1, "transactionHash": bytes.fromhex(tx_hash[2:]), "logs": [
            {"address": USDC, "topics": [bytes.fromhex(t[2:]) for t in [AUTH_USED,
             topic_address(self.sale["payer"]), self.auth["nonce"]]], "data": b""},
            {"address": USDC, "topics": [bytes.fromhex(t[2:]) for t in [TRANSFER,
             topic_address(self.sale["payer"]), topic_address(self.sale["profile"]["pay_to"])]],
             "data": (MAX_AMOUNT + int(self.bad)).to_bytes(32, "big")}]}


class SepoliaMerchantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.buyer, self.seller = Account.create(), Account.create()
        self.now = 1790900000
        self.sale = {"profile": {"network": "eip155:421614", "asset": USDC, "rpc_url": RPC,
            "url": "https://seller.example/commerce-sepolia/data", "pay_to": self.seller.address,
            "token_name": "USD Coin", "token_version": "2", "max_timeout_seconds": 120,
            "finality": "finalized", "data_version": "block-1"}, "payment_id": "payment-test",
            "payer": self.buyer.address, "amount_atoms": MAX_AMOUNT,
            "request": {"terms_hash": "ab" * 32, "data_version": "block-1", "units": 1,
                        "purpose": "research", "license": "internal-use"}, "data": {"block_number": 1}}
        self.chain = SellerChain(self.sale)
        self.path = Path(self.temp.name) / "merchant.db"
        self.merchant = Merchant(self.sale, self.chain, self.path, lambda: self.now)
        self.headers = {"idempotency-key": "payment-test"}
        self.sleep = patch("sepolia_merchant.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.temp.cleanup()

    def signed(self, **changes):
        auth = {"from": self.buyer.address.lower(), "to": self.seller.address.lower(), "value": str(MAX_AMOUNT),
                "validAfter": str(self.now - 5), "validBefore": str(self.now + 120), "nonce": "0x" + "cd" * 32,
                **changes}
        signature = Account.sign_message(encode_typed_data(full_message=typed_authorization(self.merchant.binding, auth)),
                                         private_key=self.buyer.key)
        return header({"x402Version": 2, "resource": {"url": self.merchant.binding.resource_url},
                       "accepted": self.merchant.requirement(),
                       "payload": {"authorization": auth, "signature": hexbytes(signature.signature)}})

    def send(self, encoded=None):
        return self.merchant.handle(self.sale["request"], {**self.headers,
                                    "payment-signature": encoded or self.signed()})

    def test_unsigned_challenge_does_not_sign_or_pay(self):
        status, headers, _ = self.merchant.handle(self.sale["request"], self.headers)
        self.assertEqual(status, 402)
        self.assertEqual(decode_header(headers["PAYMENT-REQUIRED"])["accepts"], [self.merchant.requirement()])
        self.assertEqual(self.chain.signed, 0)
        self.assertEqual(self.chain.broadcasts, 0)

    def test_live_startup_verifies_domain_without_an_authorization(self):
        adapter = object.__new__(SepoliaChain)
        adapter.account = self.seller
        adapter.w3, adapter.token = Mock(), Mock()
        adapter.w3.eth.chain_id = 421614
        adapter.token.functions.decimals.return_value.call.return_value = 6
        domain = {"name": "USD Coin", "version": "2", "chainId": 421614, "verifyingContract": USDC}
        adapter.token.functions.DOMAIN_SEPARATOR.return_value.call.return_value = hash_domain(domain)
        adapter.verify_network(self.merchant.binding)
        adapter.token.functions.DOMAIN_SEPARATOR.return_value.call.return_value = bytes(32)
        with self.assertRaises(MachineError):
            adapter.verify_network(self.merchant.binding)
        adapter.w3.eth.chain_id = 42161
        with self.assertRaises(MachineError):
            adapter.verify_network(self.merchant.binding)

    def test_mainnet_wrong_token_and_over_cap_fail_before_signing(self):
        for changes in [{"network": "eip155:42161"}, {"asset": "0x" + "11" * 20}, {"rpc_url": "https://other.example"}]:
            sale = copy.deepcopy(self.sale)
            sale["profile"].update(changes)
            with self.assertRaises(MachineError):
                validate_sale(sale)
        for amount in [True, 0, MAX_AMOUNT + 1]:
            sale = {**self.sale, "amount_atoms": amount}
            with self.assertRaises(MachineError):
                validate_sale(sale)
        self.assertEqual(self.chain.signed, 0)

    def test_changed_terms_order_recipient_and_amount_cannot_spend_gas(self):
        for body, headers in [({**self.sale["request"], "license": "redistribution"}, self.headers),
                              (self.sale["request"], {"idempotency-key": "unknown-order"})]:
            with self.assertRaises(MachineError):
                self.merchant.handle(body, headers)
        for changes in [{"to": Account.create().address}, {"value": str(MAX_AMOUNT + 1)}]:
            with self.assertRaises(MachineError):
                self.send(self.signed(**changes))
        self.assertEqual(self.chain.broadcasts, 0)

    def test_expired_or_long_lived_authorization_cannot_pay(self):
        for changes in [{"validBefore": str(self.now)}, {"validBefore": str(self.now + 121)},
                        {"validAfter": str(self.now - 1000)}]:
            with self.assertRaises(MachineError):
                self.send(self.signed(**changes))
        self.assertEqual(self.chain.signed, 0)

    def test_bad_buyer_signature_cannot_pay(self):
        payload = decode_header(self.signed())
        outsider = Account.create()
        auth = payload["payload"]["authorization"]
        signature = Account.sign_message(encode_typed_data(full_message=typed_authorization(self.merchant.binding, auth)),
                                         private_key=outsider.key)
        payload["payload"]["signature"] = hexbytes(signature.signature)
        with self.assertRaises(MachineError):
            self.send(header(payload))
        self.assertEqual(self.chain.signed, 0)

    def test_paid_http_delivery_excludes_signature_and_extra_request_fields(self):
        with TestClient(create_app(self.merchant)) as client:
            response = client.post("/commerce-sepolia/data", json=self.sale["request"],
                                   headers={**self.headers, "payment-signature": self.signed()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"terms_hash", "data_version", "data"})
        self.assertEqual(response.json()["data"], self.sale["data"])
        self.assertEqual(decode_header(response.headers["PAYMENT-RESPONSE"])["transaction"], self.chain.tx_hash)
        self.assertNotIn("signature", response.text)

    def test_signed_replay_after_expiry_or_restart_never_broadcasts_again(self):
        encoded = self.signed()
        self.assertEqual(self.send(encoded)[0], 200)
        self.now += 1000
        restarted = Merchant(self.sale, self.chain, self.path, lambda: self.now)
        self.assertEqual(restarted.handle(self.sale["request"], {**self.headers, "payment-signature": encoded})[0], 200)
        self.assertEqual(self.chain.signed, 1)
        self.assertEqual(self.chain.broadcasts, 1)
        with self.merchant.connect() as db:
            row = dict(db.execute("SELECT * FROM sales").fetchone())
        self.assertNotIn("signature", str(row))
        self.assertEqual(row["broadcasts"], 1)

    def test_ambiguous_broadcast_keeps_hash_and_never_sends_replacement(self):
        encoded = self.signed()
        self.chain.fail = True
        status, headers, body = self.send(encoded)
        self.assertEqual(status, 202)
        self.assertEqual(decode_header(headers["PAYMENT-RESPONSE"])["transaction"], self.chain.tx_hash)
        self.assertFalse(body["safe_to_retry_payment"])
        self.assertEqual(self.send(encoded)[0], 200)
        self.assertEqual(self.chain.broadcasts, 1)
        self.assertEqual(self.chain.signed, 1)

    def test_pending_or_wrong_transfer_never_delivers_data(self):
        self.chain.pending = True
        encoded = self.signed()
        self.assertNotIn("data", self.send(encoded)[2])
        self.chain.pending, self.chain.bad = False, True
        self.assertNotIn("data", self.send(encoded)[2])
        self.assertEqual(self.chain.broadcasts, 1)

    def test_parallel_signed_submits_commit_one_transaction(self):
        encoded = self.signed()
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: self.send(encoded)[0], range(4)))
        self.assertEqual(statuses, [200] * 4)
        self.assertEqual(self.chain.signed, 1)
        self.assertEqual(self.chain.broadcasts, 1)


if __name__ == "__main__":
    unittest.main()
