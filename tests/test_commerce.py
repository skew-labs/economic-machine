import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from economic_machine.values import MachineError, digest
from machine_commerce.domain import DEFAULT_REQUESTS, money_atoms, normalize_policy
from machine_commerce.providers import CommerceEngine, Workers
from machine_commerce.store import Store


class FakeRPC:
    def __init__(self, now):
        self.now = now
        self.stale = False

    def __call__(self, method, params):
        if method == "eth_chainId":
            return hex(421614)
        if method == "eth_gasPrice":
            return "0x2710"
        if method in {"eth_getBlockByNumber", "eth_getBlockByHash"}:
            return {"number": "0x100", "hash": "0x" + "ab" * 32,
                    "timestamp": hex(self.now - (400 if self.stale else 3))}
        raise ValueError("unexpected method")


class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1790900000
        self.store = Store(Path(self.temp.name) / "commerce.db", lambda: self.now)
        self.sid, self.token = self.store.create_session()
        self.policy = self.new_policy()
        self.rpc = FakeRPC(self.now)
        self.workers = Workers(lambda: self.now, self.rpc)
        self.engine = CommerceEngine(self.store, self.workers)

    def tearDown(self):
        self.temp.cleanup()

    def new_policy(self, **changes):
        raw = {"budget": "2", "max_order": "0.25",
               "allowed_offers": ["csv-normalize", "arbitrum-state"], "ttl_seconds": 3600}
        return self.store.create_policy(self.sid, {**raw, **changes})

    def reserve(self, key="one", offer="csv-normalize", request=None, policy=None):
        return self.store.reserve(self.sid, (policy or self.policy)["id"], offer,
            copy.deepcopy(DEFAULT_REQUESTS[offer] if request is None else request), key)

    def test_exact_money_rejects_rounding_floats_and_sub_atoms(self):
        self.assertEqual(money_atoms("0.12"), 120000)
        for value in [0.12, "0.0000001", "1e2", "NaN", "-1", "10000000000000000000000000"]:
            with self.subTest(value=value), self.assertRaises(MachineError):
                money_atoms(value)

    def test_order_cap_and_service_allowlist(self):
        narrow = self.new_policy(max_order="0.1", allowed_offers=["arbitrum-state"])
        with self.assertRaisesRegex(MachineError, "outside"):
            self.reserve(policy=narrow)
        tiny = self.new_policy(max_order="0.1")
        with self.assertRaisesRegex(MachineError, "per-order"):
            self.reserve(policy=tiny)
        self.assertEqual(self.store.snapshot(self.sid)["balance"], "10")

    def test_verified_delivery_releases_exact_provider_payment_and_fee(self):
        order = self.reserve()
        self.assertEqual(self.store.snapshot(self.sid)["reserved"], "0.12")
        done = self.engine.run(self.sid, order["id"])
        self.assertEqual(done["status"], "SETTLED")
        self.assertEqual(done["artifact"]["rows"][0]["amount"], "1200")
        receipt = done["receipt"]
        self.assertEqual(receipt["provider_amount"], "0.1188")
        self.assertEqual(receipt["platform_fee"], "0.0012")
        self.assertIsNone(receipt["tx_hash"])
        self.assertEqual(receipt["settlement"], "SANDBOX_LEDGER")
        unsealed = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        self.assertEqual(digest(unsealed), receipt["receipt_hash"])
        snapshot = self.store.snapshot(self.sid)
        self.assertEqual((snapshot["balance"], snapshot["reserved"], snapshot["spent"]), ("9.88", "0", "0.12"))

    def test_duplicate_order_and_run_pay_exactly_once(self):
        a = self.reserve()
        b = self.reserve()
        self.assertEqual(a["id"], b["id"])
        first = self.engine.run(self.sid, a["id"])
        second = self.engine.run(self.sid, a["id"])
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual(self.store.snapshot(self.sid)["spent"], "0.12")
        with self.assertRaisesRegex(MachineError, "different order"):
            self.reserve(offer="arbitrum-state")

    def test_concurrent_duplicates_and_budget_reservations_are_atomic(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            orders = list(pool.map(lambda _: self.reserve(), range(6)))
        self.assertEqual(len({o["id"] for o in orders}), 1)
        small = self.new_policy(budget="0.12", max_order="0.12")

        def attempt(i):
            try:
                return self.reserve(key="race" + str(i), policy=small)["id"]
            except MachineError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            accepted = list(pool.map(attempt, range(4)))
        self.assertEqual(sum(o is not None for o in accepted), 1)

    def test_concurrent_run_has_one_delivery_and_one_payment(self):
        order = self.reserve()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.engine.run(self.sid, order["id"]), range(4)))
        final = self.store.order(self.sid, order["id"])
        self.assertEqual(final["status"], "SETTLED")
        self.assertEqual(self.store.snapshot(self.sid)["spent"], "0.12")
        self.assertEqual([e["kind"] for e in self.store.order_events(self.sid, order["id"])],
            ["RESERVED", "FULFILLING", "DELIVERED", "VERIFIED", "SETTLED"])

    def test_tampered_result_refunds_without_provider_payment(self):
        class BadWorker(Workers):
            def fulfill(worker, offer, request):
                artifact = super().fulfill(offer, request)
                artifact["rows"][0]["amount"] = "99999"
                return artifact
        order = self.reserve()
        done = CommerceEngine(self.store, BadWorker(lambda: self.now)).run(self.sid, order["id"])
        self.assertEqual(done["status"], "REFUNDED")
        self.assertFalse(done["verification"]["accepted"])
        self.assertIsNone(done["receipt"])
        self.assertEqual(self.store.snapshot(self.sid)["balance"], "10")

    def test_provider_failure_and_bad_csv_refund_reservation(self):
        request = copy.deepcopy(DEFAULT_REQUESTS["csv-normalize"])
        request["csv"] = "asset,amount,currency\nAsset,NaN,USDC\n"
        done = self.engine.run(self.sid, self.reserve(request=request)["id"])
        self.assertEqual(done["status"], "REFUNDED")
        self.assertEqual(self.store.snapshot(self.sid)["reserved"], "0")

    def test_expired_policy_and_crashed_worker_are_recovered(self):
        order = self.reserve()
        claim = self.store.claim(self.sid, order["id"])
        self.now += 31
        reopened = Store(self.store.path, lambda: self.now)
        self.assertEqual(reopened.recover_expired(self.sid), 1)
        self.assertEqual(reopened.snapshot(self.sid)["balance"], "10")
        with self.assertRaisesRegex(MachineError, "lease"):
            reopened.complete(self.sid, order["id"], claim["lease"], {}, {})
        self.now += 3600
        with self.assertRaisesRegex(MachineError, "active"):
            self.reserve(key="expired")

    def test_cancel_does_not_reverse_settled_payment(self):
        order = self.reserve()
        self.store.cancel(self.sid, order["id"])
        self.store.cancel(self.sid, order["id"])
        order = self.reserve(key="second")
        self.engine.run(self.sid, order["id"])
        self.now += 35
        with self.assertRaisesRegex(MachineError, "transition"):
            self.store.cancel(self.sid, order["id"])
        self.assertEqual(self.store.snapshot(self.sid)["spent"], "0.12")

    def test_buyer_scoping_and_bearer_expiry(self):
        order = self.reserve()
        other, _ = self.store.create_session()
        with self.assertRaisesRegex(MachineError, "not found"):
            self.store.order(other, order["id"])
        with self.assertRaisesRegex(MachineError, "active"):
            self.store.reserve(other, self.policy["id"], "csv-normalize", DEFAULT_REQUESTS["csv-normalize"], "x")
        self.assertEqual(self.store.authenticate(self.token), self.sid)
        self.now += 86401
        with self.assertRaisesRegex(MachineError, "expired"):
            self.store.authenticate(self.token)

    def test_journal_tampering_prevents_new_spend(self):
        order = self.reserve()
        with self.store.connect() as db:
            db.execute("UPDATE events SET event_hash=? WHERE ordinal=1", ("0" * 64,))
        with self.assertRaisesRegex(MachineError, "integrity"):
            self.engine.run(self.sid, order["id"])

    def test_arbitrum_snapshot_is_source_bound_and_stale_data_is_refunded(self):
        done = self.engine.run(self.sid, self.reserve(offer="arbitrum-state")["id"])
        self.assertEqual(done["status"], "SETTLED")
        self.assertEqual(done["artifact"]["chain_id"], 421614)
        self.assertIn("NOT_INDEPENDENT", done["verification"]["assurance"])
        self.rpc.stale = True
        second = self.engine.run(self.sid, self.reserve(key="stale", offer="arbitrum-state")["id"])
        self.assertEqual(second["status"], "REFUNDED")
        self.assertEqual(self.store.snapshot(self.sid)["spent"], "0.04")

    def test_request_size_width_and_missing_keys_rejected(self):
        for request in [{}, {"csv": "x", "columns": ["a"], "numeric_columns": []},
                        {"csv": "x" * 40001, "columns": ["a"], "numeric_columns": []}]:
            with self.subTest(request=str(request)[:20]):
                try:
                    order = self.reserve(key="bad" + str(len(str(request))), request=request)
                except MachineError:
                    continue
                self.assertEqual(self.engine.run(self.sid, order["id"])["status"], "REFUNDED")
        for raw in [{"budget": "2"}, {"budget": "1", "max_order": "2",
            "allowed_offers": ["csv-normalize"], "ttl_seconds": 3600}]:
            with self.assertRaises(MachineError):
                normalize_policy(raw)


if __name__ == "__main__":
    unittest.main()
