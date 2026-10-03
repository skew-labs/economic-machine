"""Controlled exact-EVM facilitator fixtures, never live money or keys."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from eth_account import Account
from eth_account.messages import encode_typed_data
from fastapi.testclient import TestClient

import test_checkout as fixtures
from economic_machine.values import MachineError, canonical, digest
from machine_commerce.merchant import HostedMerchant, encoded
from machine_commerce.payments import typed_authorization
from machine_commerce.x402 import PaymentBinding, decode_header
from machine_commerce.api import create_app
from machine_commerce.operations import Settings

RID = "atlas-monthly"
BASE = "https://facilitator.example"
TX = "0x" + "aa" * 32


class Facilitator:
    def __init__(self, payer):
        self.payer, self.calls, self.valid, self.timeout = payer, [], True, False

    def call(self, url, body, headers=None):
        self.calls.append((url, body))
        if url.endswith("/verify"):
            return 200, {}, canonical({"isValid": self.valid, "payer": self.payer})
        if self.timeout:
            raise TimeoutError("controlled settle timeout")
        return 200, {}, canonical({"success": True, "network": "eip155:421614", "transaction": TX})


class Chain:
    status = "PENDING"
    finality = "finalized"

    def observe(self, profile, auth, tx, block):
        return {"status": self.status, "finality": self.finality, "tx_hash": TX}


class HostedMerchantTests(unittest.TestCase):
    setUpBase = fixtures.CheckoutTests.setUp
    tearDown = fixtures.CheckoutTests.tearDown

    def setUp(self):
        self.setUpBase()
        self.account = Account.from_key("0x" + "01" * 32)
        self.payer = self.account.address.lower()
        self.mandate = self.payments.mandate(self.buyer, {"payer": self.payer,
            "payment_asset": self.asset, "budget": "20", "max_order": "10",
            "resources": [RID], "ttl_seconds": 3600})
        self.payments.profiles[RID]["url"] = "https://machine.example/api/commerce/merchant/atlas-monthly"
        self.transport = Facilitator(self.payer)
        self.chain = Chain()
        self.payments.chain = self.chain
        self.config = {RID: {"facilitator_url": BASE}}
        self.merchant = HostedMerchant(self.checkout, self.config, self.transport)
        q = self.checkout.quote(self.buyer, self.raw)
        self.order = self.checkout.prepare(self.buyer, q["id"], self.mandate["id"])
        self.pid = self.order["payment_id"]
        with self.store.connect() as db:
            self.body = json.loads(db.execute("SELECT body FROM payments WHERE id=?", (self.pid,)).fetchone()[0])

    def signed(self):
        requirement = self.merchant.requirement(self.body)
        auth = self.body["authorization"]
        sig = "0x" + self.account.sign_message(encode_typed_data(full_message=typed_authorization(
            PaymentBinding(**self.body["binding"]), auth))).signature.hex()
        payload = {"x402Version": 2, "resource": {"url": self.body["binding"]["resource_url"]},
                   "accepted": requirement, "payload": {"signature": sig, "authorization": auth}}
        self.body.update(accepted=requirement, start_block=10)
        with self.store.connect() as db:
            db.execute("UPDATE payments SET body=?,status='SUBMITTED',signature_hash=? WHERE id=?",
                       (canonical(self.body).decode(), digest(payload), self.pid))
        return encoded(payload)

    def test_missing_configuration_cannot_open_an_unapproved_seller(self):
        m = HostedMerchant(self.checkout)
        with self.assertRaises(MachineError): m.handle(RID, self.pid, self.body["request"])
        with self.assertRaises(MachineError): HostedMerchant(self.checkout, {RID: {"facilitator_url": "http://localhost"}})
        with self.assertRaises(MachineError): HostedMerchant(self.checkout, {"unregistered": {"facilitator_url": BASE}})

    def test_unsigned_challenge_is_bound_to_existing_checkout_and_exact_ten_usdc(self):
        status, headers, _ = self.merchant.handle(RID, self.pid, self.body["request"])
        self.assertEqual(status, 402)
        option = decode_header(headers["PAYMENT-REQUIRED"])["accepts"][0]
        self.assertEqual(option["amount"], "10000000")
        self.assertEqual(option["payTo"], self.profile["pay_to"])
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(MachineError): self.merchant.handle(RID, "unknown", self.body["request"])
        with self.assertRaises(MachineError): self.merchant.handle(RID, self.pid, self.body["request"] | {"units": 2})
        self.now += 61
        with self.assertRaises(MachineError): self.merchant.handle(RID, self.pid, self.body["request"])

    def test_exact_signed_authorization_gets_one_settle_and_finalized_grant(self):
        signature = self.signed()
        self.chain.status = "PAID"
        result = self.merchant.handle(RID, self.pid, self.body["request"], signature)
        self.assertEqual(result[0], 200)
        self.assertEqual(result[2]["data"]["subscription_grant"]["payer"], self.payer)
        self.assertEqual(decode_header(result[1]["PAYMENT-RESPONSE"])["transaction"], TX)
        self.assertEqual([u for u, _ in self.transport.calls], [BASE + "/verify", BASE + "/settle"])
        self.assertEqual(self.payments.reconcile(self.buyer, self.pid)["status"], "SETTLED")
        first = self.checkout.get(self.buyer, self.order["id"])["entitlement"]
        self.now += 121
        self.assertEqual(self.merchant.handle(RID, self.pid, self.body["request"], signature)[0], 200)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.checkout.get(self.buyer, self.order["id"])["entitlement"], first)
        limit = next(m for m in self.payments.snapshot(self.buyer)["mandates"] if m["id"] == self.mandate["id"])
        self.assertEqual((limit["spent"], limit["reserved"]), (10_000_000, 0))

    def test_pending_receipt_and_latest_block_cannot_grant_access(self):
        signature = self.signed()
        self.assertEqual(self.merchant.handle(RID, self.pid, self.body["request"], signature)[0], 202)
        self.chain.status, self.chain.finality = "PAID", "latest"
        self.assertEqual(self.merchant.deliver(RID, self.pid)[0], 202)
        self.assertIsNone(self.checkout.get(self.buyer, self.order["id"])["entitlement"])

    def test_timeout_restart_and_nonce_readback_recover_without_resending_signature(self):
        signature = self.signed()
        self.transport.timeout = True
        self.assertEqual(self.merchant.handle(RID, self.pid, self.body["request"], signature)[0], 202)
        restarted = HostedMerchant(self.checkout, self.config, self.transport)
        self.assertEqual(restarted.handle(RID, self.pid, self.body["request"], signature)[0], 202)
        self.assertEqual(len(self.transport.calls), 2)
        self.chain.status = "PAID"
        self.assertEqual(restarted.recover(), {"scanned": 1, "delivered": 1, "failures": 0})
        self.assertEqual(self.payments.reconcile(self.buyer, self.pid)["status"], "SETTLED")
        self.assertEqual(self.checkout.get(self.buyer, self.order["id"])["entitlement"]["status"], "ACTIVE")
        self.assertEqual(len(self.transport.calls), 2)

    def test_rejected_verification_and_changed_signature_never_settle(self):
        signature = self.signed()
        self.transport.valid = False
        with self.assertRaises(MachineError): self.merchant.handle(RID, self.pid, self.body["request"], signature)
        self.assertTrue(all(url.endswith("/verify") for url, _ in self.transport.calls))
        payload = decode_header(signature); payload["payload"]["authorization"]["value"] = "1"
        with self.assertRaises(MachineError): self.merchant.handle(RID, self.pid, self.body["request"], encoded(payload))

    def test_concurrent_signed_requests_only_transmit_one_settlement(self):
        signature = self.signed()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.merchant.handle(RID, self.pid, self.body["request"], signature), range(3)))
        self.assertTrue(all(r[0] == 202 for r in results))
        self.assertEqual(sum(url.endswith("/settle") for url, _ in self.transport.calls), 1)
        with self.store.connect() as db:
            persisted = json.dumps([dict(r) for r in db.execute("SELECT * FROM merchant_attempts")])
        self.assertNotIn(decode_header(signature)["payload"]["signature"], persisted)

    def test_http_route_unsigned_challenge_and_signed_finalized_delivery(self):
        app = create_app(self.path, clock=lambda: self.now,
            settings=Settings(resources={RID: self.payments.profiles[RID]}),
            payment_chain=self.chain, merchant_config=self.config, merchant_transport=self.transport)
        client = TestClient(app)
        route = "/api/commerce/merchant/" + RID
        headers = {"Idempotency-Key": self.pid}
        response = client.post(route, json=self.body["request"], headers=headers)
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.headers["cache-control"], "no-store")
        signature = self.signed()
        self.chain.status = "PAID"
        response = client.post(route, json=self.body["request"], headers=headers | {"PAYMENT-SIGNATURE": signature})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["subscription_grant"]["plan_id"], RID)
        self.assertEqual(client.post('/api/commerce/compute/gate402-inference/check', json={}).status_code, 401)

    def test_managed_offer_refresh_is_immutable_and_preserves_inflight_quote(self):
        self.payments.profiles[RID]["seller_owner"] = "merchant-" + RID
        managed = HostedMerchant(self.checkout, self.config, self.transport)
        self.assertEqual(managed.refresh_offers(), 1)
        self.assertEqual(managed.refresh_offers(), 0)
        offers = self.checkout.catalog()["products"][0]["offers"]
        self.assertEqual(len(offers), 1)
        quote = self.checkout.quote(self.buyer, self.raw | {"supply_id": offers[0]["supply_id"],
            "max_refresh_seconds": 3600, "idempotency_key": "managed-quote"})
        self.now += 1
        self.assertEqual(managed.refresh_offers(), 0)
        self.market.agreement(self.buyer, quote["agreement"]["id"], quote["agreement"]["terms_hash"])
        self.now += 2700
        self.assertEqual(managed.refresh_offers(), 1)
        old = next(o for o in self.checkout.catalog()["products"][0]["offers"] if o["supply_id"] == offers[0]["supply_id"])
        self.assertEqual(old["updated_at"], offers[0]["updated_at"])

    def test_managed_publisher_does_not_take_over_an_existing_session(self):
        owner = "merchant-" + RID
        self.payments.profiles[RID]["seller_owner"] = owner
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET id=? WHERE id=?", (owner, self.other))
        with self.assertRaises(MachineError): self.merchant.refresh_offers()

    def test_expired_unpaid_attempt_is_not_recovered_or_charged_again(self):
        signature = self.signed()
        self.merchant.handle(RID, self.pid, self.body["request"], signature)
        with self.store.connect() as db:
            db.execute("UPDATE payments SET status='EXPIRED_UNPAID' WHERE id=?", (self.pid,))
        self.assertEqual(self.merchant.recover(), {"scanned": 0, "delivered": 0, "failures": 0})
        self.assertEqual(len(self.transport.calls), 2)

    def test_recovery_isolates_a_broken_resource_and_never_resends(self):
        from unittest.mock import patch
        signature = self.signed()
        self.merchant.handle(RID, self.pid, self.body["request"], signature)
        with patch.object(self.merchant, 'deliver', side_effect=MachineError('registry changed')):
            self.assertEqual(self.merchant.recover(), {"scanned": 1, "delivered": 0, "failures": 1})
        self.assertEqual(len(self.transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
