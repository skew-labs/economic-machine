"""Actual EIP-712 signatures and PyEVM token movement across the x402 runtime."""

import base64
import copy
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_tester import EthereumTester, PyEVMBackend
from fastapi.testclient import TestClient
from test_market import demand, supply
from web3 import EthereumTesterProvider, Web3
from web3.exceptions import TransactionNotFound

from economic_machine.values import MachineError
from machine_commerce.api import create_app
from machine_commerce.market import Market
from machine_commerce.operations import Settings
from machine_commerce.payments import Payments
from machine_commerce.store import Store
from machine_commerce.transport import AUTH_USED, TRANSFER, Chain, PinnedTransport, topic_address

ROOT = Path(__file__).resolve().parents[1]


def header(body):
    return base64.b64encode(json.dumps(body).encode()).decode()


def hexbytes(value):
    return "0x" + bytes(value).hex()


class EVMSeller:
    def __init__(self, w3, token, profile):
        self.w3, self.token, self.profile = w3, token, profile
        self.calls, self.signed_calls = 0, 0
        self.drop = self.bad_delivery = self.wrong_receipt = False
        self.rpc_off = self.no_submit = False

    def log(self, log):
        return {"address": log["address"], "topics": [hexbytes(t) for t in log["topics"]],
                "data": hexbytes(log["data"]), "blockHash": hexbytes(log["blockHash"]),
                "transactionHash": hexbytes(log["transactionHash"]), "removed": False}

    def rpc(self, method, params):
        if method == "eth_chainId":
            return hex(self.w3.eth.chain_id)
        if method == "eth_blockNumber":
            return hex(self.w3.eth.block_number)
        if method == "eth_getCode":
            return hexbytes(self.w3.eth.get_code(Web3.to_checksum_address(params[0])))
        if method == "eth_call":
            return hexbytes(self.w3.eth.call({**params[0], "to": Web3.to_checksum_address(params[0]["to"])},
                block_identifier=int(params[1], 16) if params[1].startswith("0x") else params[1]))
        if method == "eth_getBlockByNumber":
            block = self.w3.eth.get_block("latest" if params[0] == "finalized" else int(params[0], 16))
            return {"number": hex(block.number), "timestamp": hex(block.timestamp), "hash": hexbytes(block.hash)}
        if method == "eth_getTransactionReceipt":
            try:
                receipt = self.w3.eth.get_transaction_receipt(params[0])
            except TransactionNotFound:
                return None
            return {"transactionHash": hexbytes(receipt.transactionHash), "blockNumber": hex(receipt.blockNumber),
                "blockHash": hexbytes(receipt.blockHash), "status": hex(receipt.status), "logs": [self.log(l) for l in receipt.logs]}
        if method == "eth_getLogs":
            rule = params[0]
            logs = self.w3.eth.get_logs({**rule, "address": Web3.to_checksum_address(rule["address"]),
                "fromBlock": int(rule["fromBlock"], 16), "toBlock": int(rule["toBlock"], 16)})
            return [self.log(l) for l in logs]
        raise AssertionError(method)

    def call(self, url, body, headers=None):
        if url == self.profile["rpc_url"]:
            if self.rpc_off:
                raise httpx.ConnectError("RPC unavailable")
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": self.rpc(body["method"], body["params"])}).encode()
        self.calls += 1
        if not headers or "PAYMENT-SIGNATURE" not in headers:
            required = {"x402Version": 2, "resource": {"url": self.profile["url"]}, "accepts": [{
                "scheme": "exact", "network": self.profile["network"], "asset": self.token.address.lower(),
                "payTo": self.profile["pay_to"], "amount": "400000", "maxTimeoutSeconds": 60,
                "extra": {"name": self.profile["token_name"], "version": self.profile["token_version"]}}]}
            return 402, {"payment-required": header(required)}, b'{}'
        self.signed_calls += 1
        if self.no_submit:
            raise httpx.ConnectError("connection lost before receiving a receipt")
        payload = json.loads(base64.b64decode(headers["PAYMENT-SIGNATURE"]))["payload"]
        auth = payload["authorization"]
        signed = bytes.fromhex(payload["signature"][2:])
        tx = self.token.functions.transferWithAuthorization(Web3.to_checksum_address(auth["from"]),
            Web3.to_checksum_address(auth["to"]), int(auth["value"]), int(auth["validAfter"]),
            int(auth["validBefore"]), bytes.fromhex(auth["nonce"][2:]), signed[64], signed[:32], signed[32:64]).transact({"from": self.w3.eth.accounts[3]})
        if self.drop:
            raise httpx.ReadTimeout("response lost after broadcast")
        response = {"success": True, "network": self.profile["network"],
            "transaction": "0x" + "ff" * 32 if self.wrong_receipt else hexbytes(tx)}
        delivery = {"terms_hash": body["terms_hash"], "data_version": body["data_version"], "data": {"observed": True}}
        return 200, {"payment-response": header(response)}, (b'{}' if self.bad_delivery else json.dumps(delivery).encode())


class PaymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled = json.loads((ROOT / "artifacts/contracts.json").read_text())["TestEIP3009Token"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = PyEVMBackend()
        self.tester = EthereumTester(self.backend)
        self.w3 = Web3(EthereumTesterProvider(self.tester))
        contract = self.w3.eth.contract(abi=self.compiled["abi"], bytecode=self.compiled["bytecode"])
        receipt = self.w3.eth.wait_for_transaction_receipt(contract.constructor().transact({"from": self.w3.eth.accounts[0]}))
        self.token = self.w3.eth.contract(address=receipt.contractAddress, abi=self.compiled["abi"])
        self.token.functions.mint(self.w3.eth.accounts[0], 2_000_000).transact({"from": self.w3.eth.accounts[0]})
        self.now = self.w3.eth.get_block("latest").timestamp
        self.path = Path(self.temp.name) / "payments.db"
        self.store = Store(self.path, lambda: self.now)
        self.buyer, self.owner_token = self.store.create_session()
        self.seller, _ = self.store.create_session()
        self.market = Market(self.store)
        self.asset = f'eip155:{self.w3.eth.chain_id}/erc20:{self.token.address.lower()}'
        self.profile = {"url": "https://seller.example/data", "rpc_url": "https://rpc.example/chain",
            "network": f"eip155:{self.w3.eth.chain_id}", "asset": self.token.address,
            "pay_to": self.w3.eth.accounts[1], "token_name": "Machine Payment Test", "token_version": "1",
            "max_timeout_seconds": 60, "seller_owner": self.seller,
            "data_type": "arbitrum.block-state", "data_version": "block-1", "finality": "finalized"}
        self.transport = EVMSeller(self.w3, self.token, self.profile)
        self.payments = Payments(self.store, self.market, {"state-data": self.profile}, self.transport)
        self.market.register(self.buyer, "demand", demand(payment_asset=self.asset))
        self.supply = self.market.register(self.seller, "supply", supply(self.now, payment_asset=self.asset))
        self.match = self.market.snapshot(self.buyer)["matches"][0]
        self.mandate = self.payments.mandate(self.buyer, {"payer": self.w3.eth.accounts[0], "payment_asset": self.asset,
            "budget": "1", "max_order": "0.5", "resources": ["state-data"], "ttl_seconds": 3600})
        self.raw = {"match_id": self.match["id"], "terms_hash": self.match["terms_hash"],
            "mandate_id": self.mandate["id"], "resource_id": "state-data", "idempotency_key": "payment-one"}

    def tearDown(self):
        self.temp.cleanup()

    def ready(self):
        prepared = self.payments.prepare(self.buyer, self.raw)
        return self.payments.challenge(self.buyer, prepared["id"])

    def signed(self, ready, account=None):
        signed = Account.sign_message(encode_typed_data(full_message=ready["typed_data"]),
            private_key=account or self.backend.account_keys[0])
        payload = copy.deepcopy(ready["payment_template"])
        payload["payload"]["signature"] = hexbytes(signed.signature)
        return header(payload)

    def test_customer_signature_exact_transfer_delivery_and_final_receipt(self):
        ready = self.ready()
        done = self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        self.assertEqual(done["status"], "SETTLED")
        self.assertEqual(done["observation"]["status"], "PAID")
        self.assertEqual(self.token.functions.balanceOf(self.w3.eth.accounts[1]).call(), 400000)
        self.assertEqual(self.token.functions.balanceOf(self.w3.eth.accounts[0]).call(), 1600000)
        mandate = self.payments.snapshot(self.buyer)["mandates"][0]
        self.assertEqual((mandate["reserved"], mandate["spent"]), (0, 400000))
        self.assertNotIn("signature", json.dumps(done))
        self.assertEqual(self.store.snapshot(self.buyer)["spent"], "0")
        (ROOT / "artifacts/x402-evm.json").write_text(json.dumps({
            "environment": "LOCAL_PYEVM_EPHEMERAL_TEST_TOKEN", "public_chain_payment": False,
            "customer_signature": "EIP712_TRANSFER_WITH_AUTHORIZATION", "payment": done,
            "payer_balance_atoms": self.token.functions.balanceOf(self.w3.eth.accounts[0]).call(),
            "recipient_balance_atoms": self.token.functions.balanceOf(self.w3.eth.accounts[1]).call(),
            "mandate": {k: mandate[k] for k in ["reserved", "spent", "budget"]},
            "test_merchant": True, "secret_or_signature_stored": False}, indent=2) + "\n")

    def test_concurrent_submit_has_one_broadcast_and_one_charge(self):
        ready = self.ready()
        signed = self.signed(ready)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.payments.submit(self.buyer, ready["id"], signed), range(4)))
        self.assertEqual(self.transport.signed_calls, 1)
        self.assertEqual(self.payments.get(self.buyer, ready["id"])["status"], "SETTLED")
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["spent"], 400000)

    def test_lost_response_recovers_by_nonce_without_resending_or_fabricating_delivery(self):
        ready = self.ready()
        self.transport.drop = True
        done = self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        self.assertEqual(done["status"], "PAID_DELIVERY_MISSING")
        restarted = Payments(Store(self.path, lambda: self.now), self.market, {"state-data": self.profile}, self.transport)
        restarted.submit(self.buyer, ready["id"], self.signed(ready))
        self.assertEqual(self.transport.signed_calls, 1)
        self.assertFalse(done["safe_to_retry_payment"])

    def test_wrong_claimed_transaction_is_replaced_by_independent_nonce_evidence(self):
        ready = self.ready()
        self.transport.wrong_receipt = True
        done = self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        self.assertEqual(done["status"], "SETTLED")
        self.assertNotEqual(done["tx_hash"], "0x" + "ff" * 32)

    def test_rpc_outage_retains_hold_and_restart_reconciles_without_another_payment(self):
        ready = self.ready()
        self.transport.drop = self.transport.rpc_off = True
        result = self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 400000)
        restarted = Payments(Store(self.path, lambda: self.now), self.market, {"state-data": self.profile}, self.transport)
        restarted.recover()
        self.assertEqual(self.transport.signed_calls, 1)
        self.transport.rpc_off = False
        restarted.recover()
        self.assertEqual(restarted.get(self.buyer, ready["id"])["status"], "PAID_DELIVERY_MISSING")
        self.assertIsNone(restarted.get(self.buyer, ready["id"])["reason"])
        self.assertEqual(self.transport.signed_calls, 1)

    def test_recovery_profile_failure_is_reported_and_keeps_capital_held(self):
        ready = self.ready()
        self.transport.no_submit = self.transport.rpc_off = True
        self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        self.payments.profiles["state-data"]["data_version"] = "operator-changed-profile"
        result = self.payments.recover()
        self.assertEqual(result, {"inspected": 1, "failures": 1})
        self.assertEqual(self.payments.get(self.buyer, ready["id"])["reason"], "PAYMENT_REVIEW_REQUIRED")
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 400000)
        self.assertEqual(self.transport.signed_calls, 1)

    def test_recovery_failure_degrades_http_readiness(self):
        ready = self.ready()
        self.transport.no_submit = self.transport.rpc_off = True
        self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        changed = {**self.profile, "data_version": "operator-changed-profile"}
        app = create_app(self.path, lambda: self.now, settings=Settings(resources={"state-data": changed}),
                         payment_transport=self.transport)
        with TestClient(app) as client:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                response = client.get("/healthz")
                if response.status_code == 503:
                    break
                time.sleep(.02)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["payment_worker_error"], "PAYMENT_RECOVERY_DEGRADED")
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 400000)

    def test_expired_unpaid_requires_finalized_unused_nonce_before_releasing_hold(self):
        ready = self.ready()
        self.transport.no_submit = True
        pending = self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        self.assertEqual(pending["status"], "UNKNOWN")
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 400000)
        self.tester.time_travel(ready["expires"] + 2)
        self.tester.mine_block()
        self.now = self.w3.eth.get_block("latest").timestamp
        done = self.payments.reconcile(self.buyer, ready["id"])
        self.assertEqual(done["status"], "EXPIRED_UNPAID")
        self.assertEqual((self.payments.snapshot(self.buyer)["mandates"][0]["reserved"],
                          self.payments.snapshot(self.buyer)["mandates"][0]["spent"]), (0, 0))
        self.assertEqual(self.transport.signed_calls, 1)

    def test_concurrent_agreements_share_one_capital_budget(self):
        self.payments.prepare(self.buyer, self.raw)
        for index in range(2):
            registered = self.market.register(self.buyer, "demand", demand(payment_asset=self.asset))
            match = self.market.demand_matches(self.buyer, registered["id"])["matches"][0]
            raw = {**self.raw, "match_id": match["id"], "terms_hash": match["terms_hash"],
                   "idempotency_key": "budget-" + str(index)}
            if index == 0:
                self.payments.prepare(self.buyer, raw)
            else:
                with self.assertRaises(MachineError):
                    self.payments.prepare(self.buyer, raw)
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 800000)

    def test_test_credit_agreement_cannot_authorize_real_token_spend(self):
        registered = self.market.register(self.buyer, "demand", demand())
        self.market.register(self.seller, "supply", supply(self.now))
        matches = self.market.demand_matches(self.buyer, registered["id"])["matches"]
        match = next(item for item in matches if item["status"] == "AGREED")
        with self.assertRaises(MachineError):
            self.payments.prepare(self.buyer, {**self.raw, "match_id": match["id"],
                "terms_hash": match["terms_hash"], "idempotency_key": "deny-credit"})
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 0)

    def test_bad_delivery_is_paid_missing_and_never_reported_as_refunded(self):
        ready = self.ready()
        self.transport.bad_delivery = True
        result = self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        self.assertEqual(result["status"], "PAID_DELIVERY_MISSING")
        with self.assertRaises(MachineError):
            self.payments.cancel_unsigned(self.buyer, ready["id"])

    def test_changed_terms_or_wrong_signer_never_transmits_authorization(self):
        ready = self.ready()
        with self.assertRaises(MachineError):
            self.payments.submit(self.buyer, ready["id"], self.signed(ready, self.backend.account_keys[2]))
        payload = copy.deepcopy(ready["payment_template"])
        payload["payload"]["signature"] = "0x" + "11" * 65
        payload["payload"]["authorization"]["value"] = "400001"
        with self.assertRaises(MachineError):
            self.payments.submit(self.buyer, ready["id"], header(payload))
        self.assertEqual(self.transport.signed_calls, 0)

    def test_merchant_and_registry_cannot_substitute_an_onchain_signing_domain(self):
        self.profile["token_name"] = "Wrong token domain"
        self.payments.profiles["state-data"]["token_name"] = self.profile["token_name"]
        with self.assertRaises(MachineError):
            self.ready()
        self.assertEqual(self.transport.signed_calls, 0)

    def test_unfunded_external_wallet_cannot_reach_signing_admission(self):
        mandate = self.payments.mandate(self.buyer, {"payer": self.w3.eth.accounts[2], "payment_asset": self.asset,
            "budget": "1", "max_order": "0.5", "resources": ["state-data"], "ttl_seconds": 3600})
        self.raw["mandate_id"] = mandate["id"]
        with self.assertRaises(MachineError):
            self.ready()
        self.assertEqual(self.transport.signed_calls, 0)

    def test_version_change_invalidates_ready_payment(self):
        ready = self.ready()
        self.market.refresh_supply(self.seller, self.supply["id"], "block-2")
        with self.assertRaises(MachineError):
            self.payments.submit(self.buyer, ready["id"], self.signed(ready))
        self.assertEqual(self.transport.signed_calls, 0)

    def test_idempotency_and_unsigned_cancel_preserve_exact_hold(self):
        first = self.payments.prepare(self.buyer, self.raw)
        self.assertEqual(self.payments.prepare(self.buyer, self.raw)["id"], first["id"])
        with self.assertRaises(MachineError):
            self.payments.prepare(self.buyer, {**self.raw, "resource_id": "other"})
        with self.assertRaises(MachineError):
            self.payments.prepare(self.buyer, {**self.raw, "idempotency_key": "duplicate-agreement"})
        self.payments.cancel_unsigned(self.buyer, first["id"])
        self.payments.cancel_unsigned(self.buyer, first["id"])
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 0)

    def test_other_owner_and_registry_change_are_denied(self):
        ready = self.ready()
        with self.assertRaises(MachineError):
            self.payments.get(self.seller, ready["id"])
        self.payments.profiles["state-data"]["pay_to"] = self.w3.eth.accounts[2]
        with self.assertRaises(MachineError):
            self.payments.submit(self.buyer, ready["id"], self.signed(ready))

    def test_unsigned_expiry_recovers_reservation_without_payment(self):
        ready = self.ready()
        self.now += 61
        self.payments.recover()
        self.assertEqual(self.payments.get(self.buyer, ready["id"])["status"], "CANCELLED")
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 0)

    def test_scoped_http_client_is_bound_to_one_real_payment_mandate(self):
        app = create_app(self.path, lambda: self.now, settings=Settings(resources={"state-data": self.profile}),
                         payment_transport=self.transport)
        owner = TestClient(app, headers={"Authorization": "Bearer " + self.owner_token})
        created = owner.post("/api/keys", json={"name": "Payment agent", "scopes": ["read", "payments:request"],
            "policy_id": None, "payment_mandate_id": self.mandate["id"], "ttl_seconds": 3600})
        self.assertEqual(created.status_code, 200, created.text)
        agent = TestClient(app, headers={"Authorization": "Bearer " + created.json()["secret"]})
        denied = agent.post("/api/payment-mandates", json={})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(agent.post("/api/payments", json={**self.raw, "mandate_id": "mandate-other"}).status_code, 403)
        prepared = agent.post("/api/payments", json=self.raw)
        self.assertEqual(prepared.status_code, 200, prepared.text)
        ready = agent.post(f'/api/payments/{prepared.json()["id"]}/challenge', json={}).json()
        response = agent.post(f'/api/payments/{ready["id"]}/submit', json={"payment_signature": self.signed(ready)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "SETTLED")
        owner.close()
        agent.close()


class TransportTests(unittest.TestCase):
    def test_dns_pin_keeps_host_and_tls_identity_and_never_follows_redirect(self):
        captured = []
        delegate = httpx.MockTransport(lambda req: (captured.append(req) or httpx.Response(302, headers={"Location": "https://evil.example/"})))
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
        transport = PinnedTransport(["https://seller.example/data"], resolver, delegate)
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            result = client.post("https://seller.example/data", json={})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(captured[0].url.host, "93.184.216.34")
        self.assertEqual(captured[0].headers["Host"], "seller.example")
        self.assertEqual(captured[0].extensions["sni_hostname"], "seller.example")

    def test_private_mixed_and_unregistered_egress_is_rejected(self):
        for ips in [["127.0.0.1"], ["169.254.169.254"], ["93.184.216.34", "10.0.0.1"], ["::1"]]:
            resolver = lambda *args, addresses=ips, **kwargs: [(2, 1, 6, "", (ip, 443)) for ip in addresses]
            with httpx.Client(transport=PinnedTransport(["https://seller.example/data"], resolver, httpx.MockTransport(
                    lambda req: httpx.Response(200)))) as client, self.assertRaises(MachineError):
                client.post("https://seller.example/data", json={})
        transport = PinnedTransport(["https://seller.example/data"])
        with httpx.Client(transport=transport) as client, self.assertRaises(MachineError):
            client.post("https://evil.example/data", json={})
        transport = PinnedTransport(["https://seller.example/data?key=private"])
        with httpx.Client(transport=transport) as client, self.assertRaises(MachineError):
            client.post("https://seller.example/data?key=private", json={})

    def test_chain_requires_exact_authorization_transfer_canonical_finalized_receipt(self):
        # Focused hostile-RPC fixture: plausible txHash alone cannot move a capital hold.
        class Fake:
            def __init__(self):
                self.receipt_hash = "0x" + "44" * 32
                self.amount = 400001

            def call(inner, url, body, headers=None):
                method = body["method"]
                if method == "eth_chainId": result = "0xa4b1"
                elif method == "eth_getBlockByNumber": result = {"number": "0xa", "timestamp": "0x64", "hash": "0x" + "44" * 32}
                elif method == "eth_getLogs": result = []
                elif method == "eth_getTransactionReceipt": result = {"transactionHash": "0x" + "33" * 32,
                    "blockNumber": "0x9", "blockHash": inner.receipt_hash, "status": "0x1", "logs": [
                        {"address": "0x" + "11" * 20, "transactionHash": "0x" + "33" * 32,
                         "blockHash": inner.receipt_hash, "topics": [AUTH_USED, topic_address("0x" + "22" * 20), "0x" + "55" * 32], "data": "0x"},
                        {"address": "0x" + "11" * 20, "transactionHash": "0x" + "33" * 32,
                         "blockHash": inner.receipt_hash, "topics": [TRANSFER, topic_address("0x" + "22" * 20), topic_address("0x" + "66" * 20)], "data": hex(inner.amount)}]}
                else: raise AssertionError(method)
                return 200, {}, json.dumps({"id": 1, "result": result}).encode()
        fake = Fake()
        chain = Chain(fake)
        profile = {"rpc_url": "https://rpc.example", "network": "eip155:42161", "finality": "finalized", "asset": "0x" + "11" * 20}
        auth = {"from": "0x" + "22" * 20, "to": "0x" + "66" * 20, "value": "400000", "nonce": "0x" + "55" * 32, "validBefore": "200"}
        self.assertEqual(chain.observe(profile, auth, "0x" + "33" * 32, 0)["status"], "PENDING")
        fake.amount = 400000
        self.assertEqual(chain.observe(profile, auth, "0x" + "33" * 32, 0)["status"], "PAID")
        fake.receipt_hash = "0x" + "77" * 32
        self.assertEqual(chain.observe(profile, auth, "0x" + "33" * 32, 0)["reason"], "NONCANONICAL_RECEIPT")


if __name__ == "__main__":
    unittest.main()
