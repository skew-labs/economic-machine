import tempfile
import unittest
from pathlib import Path

from economic_machine.values import MachineError, digest
from machine_commerce.market import Market, negotiate, normalize_demand, normalize_supply
from machine_commerce.store import Store


def demand(**changes):
    return {"data_type": "arbitrum.block-state", "purpose": "research", "license": "internal-use",
            "units": 10, "max_unit_price": "0.04", "max_total_price": "0.4", "max_age_seconds": 120,
            "max_refresh_seconds": 60, "response_seconds": 30, "ttl_seconds": 3600, **changes}


def supply(now, **changes):
    return {"name": "Independent Chain Observer", "data_type": "arbitrum.block-state", "version": "block-1",
            "unit_price": "0.05", "floor_price": "0.035", "discount_bps": 2000,
            "discount_min_units": 10, "min_units": 1, "max_units": 100,
            "purposes": ["research", "automation"], "licenses": ["internal-use"],
            "updated_at": now, "refresh_seconds": 30, "response_seconds": 10, "ttl_seconds": 3600, **changes}


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1790900000
        self.store = Store(Path(self.temp.name) / "market.db", lambda: self.now)
        self.market = Market(self.store)
        self.buyer, _ = self.store.create_session()
        self.seller, _ = self.store.create_session()

    def tearDown(self):
        self.temp.cleanup()

    def pair(self, **seller_changes):
        buy = self.market.register(self.buyer, "demand", demand())
        sell = self.market.register(self.seller, "supply", supply(self.now, **seller_changes))
        return buy, sell

    def test_bilateral_policy_counter_agrees_without_llm_or_payment(self):
        _, sell = self.pair()
        self.assertEqual(sell["matching"]["pairs_evaluated"], 1)
        match = self.market.snapshot(self.buyer)["matches"][0]
        self.assertEqual(match["status"], "AGREED")
        self.assertEqual(match["terms"]["total_price"], "0.4")
        self.assertEqual(match["terms"]["unit_price"], "0.04")
        self.assertEqual(digest(match["terms"]), match["terms_hash"])
        self.assertEqual([t["kind"] for t in match["trace"]], ["OFFER", "POLICY_COUNTER", "POLICY_ACCEPT"])
        self.assertEqual(match["payment_status"], "NOT_REQUESTED")
        self.assertEqual(match["language_model_calls"], 0)
        self.assertEqual(self.store.snapshot(self.buyer)["balance"], "10")

    def test_floor_is_never_crossed_and_total_budget_is_enforced(self):
        d = normalize_demand(demand(max_unit_price="0.1", max_total_price="1"))
        s = normalize_supply(supply(self.now, discount_bps=9000), self.now)
        self.assertEqual(negotiate(d, s, self.now)["terms"]["unit_price"], "0.035")
        d["max_total_atoms"] = 349999
        result = negotiate(d, s, self.now)
        self.assertEqual(result["status"], "ESCALATE")
        self.assertIn("PRICE_OUTSIDE_BUYER_POLICY", result["reason_codes"])

    def test_each_nonprice_term_can_prevent_agreement(self):
        variants = [({"purpose": "commercial"}, {}, "PURPOSE_NOT_GRANTED"),
                    ({"license": "redistribution"}, {}, "LICENSE_NOT_GRANTED"),
                    ({"units": 101}, {}, "QUANTITY_OUTSIDE_RULES"),
                    ({}, {"updated_at": self.now - 120}, "DATA_NOT_FRESH"),
                    ({}, {"refresh_seconds": 61}, "REFRESH_TOO_SLOW"),
                    ({}, {"response_seconds": 31}, "RESPONSE_TOO_SLOW"),
                    ({"data_type": "sports.score"}, {}, "DATA_TYPE_MISMATCH")]
        for dc, sc, code in variants:
            with self.subTest(code=code):
                result = negotiate(normalize_demand(demand(**dc)), normalize_supply(supply(self.now, **sc), self.now), self.now)
                self.assertEqual(result["status"], "ESCALATE")
                self.assertIn(code, result["reason_codes"])

    def test_rejected_terms_are_visible_only_to_participants(self):
        self.pair(licenses=["commercial-use"])
        view = self.market.snapshot(self.buyer)
        self.assertEqual(view["matches"], [])
        self.assertEqual(view["deferred"][0]["reason_codes"], ["LICENSE_NOT_GRANTED"])
        stranger, _ = self.store.create_session()
        public = self.market.snapshot(stranger)
        self.assertEqual(public["deferred"], [])
        self.assertEqual(public["demands"], [])
        for secret in ["floor_atoms", "discount_bps", "discount_min_units"]:
            self.assertNotIn(secret, public["supplies"][0])

    def test_event_matching_only_evaluates_affected_type_and_no_self_trade(self):
        self.market.register(self.buyer, "demand", demand(data_type="sports.score"))
        own = self.market.register(self.buyer, "supply", supply(self.now))
        self.assertEqual(own["matching"]["pairs_evaluated"], 0)
        self.market.register(self.buyer, "demand", demand())
        sell = self.market.register(self.seller, "supply", supply(self.now))
        self.assertEqual(sell["matching"]["pairs_evaluated"], 1)

    def test_data_update_invalidates_old_terms_and_creates_new_hash(self):
        _, sell = self.pair()
        old = self.market.snapshot(self.buyer)["matches"][0]
        self.now += 10
        self.market.refresh_supply(self.seller, sell["id"], "block-2")
        new = self.market.snapshot(self.buyer)["matches"][0]
        self.assertNotEqual(old["terms_hash"], new["terms_hash"])
        self.assertEqual(new["terms"]["data_version"], "block-2")
        with self.assertRaises(MachineError):
            self.market.admit(self.buyer, old["id"], old["terms_hash"])

    def test_new_demand_does_not_recompute_other_unchanged_buyers(self):
        self.pair()
        newcomer, _ = self.store.create_session()
        registered = self.market.register(newcomer, "demand", demand())
        self.assertEqual(registered["matching"]["pairs_evaluated"], 1)
        self.assertEqual(registered["matching"]["compatible_pairs"], 1)

    def test_duplicate_update_does_not_extend_data_freshness(self):
        _, sell = self.pair()
        self.now += 119
        result = self.market.refresh_supply(self.seller, sell["id"], "block-1")
        self.assertEqual(result["trigger"], "DUPLICATE_VERSION")
        self.assertEqual(len(self.market.snapshot(self.buyer)["matches"]), 1)
        self.now += 1
        self.assertEqual(self.market.snapshot(self.buyer)["matches"], [])
        self.assertEqual(self.market.tick()["expired_agreements"], 1)
        self.assertEqual(self.market.tick()["expired_agreements"], 0)

    def test_payment_admission_checks_owner_hash_and_expiry_then_fails_closed(self):
        self.pair()
        match = self.market.snapshot(self.buyer)["matches"][0]
        for sid, thash in [(self.seller, match["terms_hash"]), (self.buyer, "a" * 64)]:
            with self.assertRaises(MachineError):
                self.market.admit(sid, match["id"], thash)
        first = self.market.admit(self.buyer, match["id"], match["terms_hash"])
        self.assertEqual(first["status"], "BLOCKED")
        self.assertIsNone(first["tx_hash"])
        self.assertEqual(first, self.market.admit(self.buyer, match["id"], match["terms_hash"]))
        self.now += 120
        with self.assertRaises(MachineError):
            self.market.admit(self.buyer, match["id"], match["terms_hash"])

    def test_foreign_refresh_and_expired_participant_rejected(self):
        _, sell = self.pair()
        with self.assertRaises(MachineError):
            self.market.refresh_supply(self.buyer, sell["id"], "stolen")
        self.now += 86401
        with self.assertRaises(MachineError):
            self.market.register(self.buyer, "demand", demand())

    def test_invalid_unhashable_and_ambiguous_fields_rejected(self):
        for changes in [{"purpose": []}, {"license": {}}, {"units": True}, {"max_unit_price": 0.04},
                        {"data_type": "../file"}]:
            with self.subTest(changes=changes), self.assertRaises(MachineError):
                normalize_demand(demand(**changes))
        for changes in [{"floor_price": "0.06"}, {"purposes": [[]]}, {"licenses": ["internal-use"] * 2},
                        {"updated_at": self.now + 6}, {"discount_bps": -1}]:
            with self.subTest(changes=changes), self.assertRaises(MachineError):
                normalize_supply(supply(self.now, **changes), self.now)

    def test_restart_preserves_policies_and_agreements(self):
        self.pair()
        before = self.market.snapshot(self.buyer)
        restarted = Market(Store(self.store.path, lambda: self.now))
        self.assertEqual(restarted.snapshot(self.buyer), before)

    def test_corrupt_journal_rolls_back_policy_registration(self):
        with self.store.connect() as db:
            db.execute("UPDATE events SET event_hash=? WHERE ordinal=1", ("0" * 64,))
        with self.assertRaisesRegex(MachineError, "integrity"):
            self.market.register(self.buyer, "demand", demand())
        self.assertEqual(self.market.snapshot(self.buyer)["demands"], [])
