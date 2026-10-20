from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from economic_machine.muse_maker import MODEL, digest, request, response
from economic_machine.muse_policy_worker import Budget, ForwardGate, atomic, packet, read_json
from test_muse_maker import observer, policy, provider


class PolicyWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)

    def tearDown(self): self.tmp.cleanup()

    def budget(self, amount="0.02", count=10):
        return Budget(self.root/"model.sqlite", "session", 100000, amount, count)

    def gate(self):
        gate = ForwardGate(10000)
        for i in range(8):
            obs = observer(); stamp = 10001+i*200
            obs["updated_wall_ms"] = stamp; obs["model"]["at_wall_ms"] = stamp; obs["model"]["version"] = i+1
            self.assertTrue(gate.add(obs, stamp))
        return gate

    def test_reserve_before_call(self):
        b = self.budget(); ident = b.reserve(request(observer(), 10000), 10000)
        self.assertEqual(ident, 1); self.assertEqual(b.totals()["unconfirmed_cost_reservations"], 1)
        self.assertGreater(float(b.totals()["cost_committed_or_reserved_usd"]), 0); b.db.close()

    def test_timeout_reservation_survives_restart(self):
        b = self.budget(); b.reserve(request(observer(), 10000), 10000); total = b.totals(); b.db.close()
        b = self.budget(); self.assertEqual(b.totals(), total); b.db.close()

    def test_same_request_not_retried_with_fresh_id(self):
        b = self.budget(); req = request(observer(), 10000); b.reserve(req, 10000)
        with self.assertRaises(sqlite3.IntegrityError): b.reserve(req, 10001)
        self.assertEqual(b.totals()["calls_reserved"], 1); b.db.close()

    def test_deadline_and_budget_exhaustion(self):
        b = self.budget("0.00001")
        with self.assertRaises(ValueError): b.reserve(request(observer(), 10000), 10000)
        with self.assertRaises(ValueError): b.reserve(request(observer(), 10000), 100001)
        b.db.close()

    def test_scope_cannot_change_on_restart(self):
        b = self.budget(); b.db.close()
        with self.assertRaises(ValueError): self.budget("0.03")

    def test_explicit_unlimited_cost_still_preserves_call_and_time_bounds(self):
        b = self.budget("unlimited", 1)
        b.reserve(request(observer(), 10000), 10000)
        with self.assertRaises(ValueError): b.reserve(request(observer(), 10001), 10001)
        self.assertEqual(b.scope["maximum_usd"], "unlimited"); b.db.close()

    def test_existing_budget_cannot_be_silently_changed_to_unlimited(self):
        b = self.budget(); b.db.close()
        with self.assertRaises(ValueError): self.budget("unlimited")

    def test_nonfinite_budget_not_an_unlimited_alias(self):
        for value in ["NaN", "Infinity", "-Infinity", "0", "-1", "bad"]:
            with self.assertRaises(ValueError): self.budget(value)

    def test_actual_cost_replaces_reservation_once(self):
        b = self.budget(); i = b.reserve(request(observer(), 10000), 10000)
        raw = json.dumps({"usage": {"cost": 0.0001}}); b.finish(i, raw, True)
        self.assertEqual(b.totals()["cost_committed_or_reserved_usd"], "0.0001")
        with self.assertRaises(ValueError): b.finish(i, raw, True)
        b.db.close()

    def test_missing_cost_keeps_reservation(self):
        b = self.budget(); i = b.reserve(request(observer(), 10000), 10000)
        before = b.totals()["cost_committed_or_reserved_usd"]; b.finish(i, "{}", False)
        self.assertEqual(b.totals()["cost_committed_or_reserved_usd"], before); b.db.close()

    def test_cost_breach_latches(self):
        b = self.budget(); i = b.reserve(request(observer(), 10000), 10000)
        with self.assertRaises(ValueError): b.finish(i, '{"usage":{"cost":0.019}}', True)
        self.assertTrue(b.totals()["cost_breach"])
        with self.assertRaises(ValueError): b.reserve(request(observer(), 10001), 10001)
        b.db.close()

    def test_forward_gate_is_causal_and_nonduplicate(self):
        gate = ForwardGate(10000); obs = observer(); obs["model"]["version"] = 1
        self.assertFalse(gate.add(obs, 10001)); self.assertFalse(gate.ready())
        obs["updated_wall_ms"] = obs["model"]["at_wall_ms"] = 10001
        self.assertTrue(gate.add(obs, 10001)); self.assertFalse(gate.add(obs, 10001))
        self.assertFalse(gate.add(obs, 20000))

    def test_valid_response_to_atomic_packet(self):
        gate = self.gate(); proposal = response(json.dumps(provider()), "hash")
        out = packet(proposal, session_id="session", version=1, issued_ms=10000, published_ms=12000, deadline_ms=100000, gate=gate)
        atomic(self.root/"policy.json", out)
        self.assertEqual(read_json(self.root/"policy.json"), out)
        self.assertEqual(out["policy_sha256"], digest(out["policy"]))
        self.assertFalse(out["forward_gate"]["profitability_proven"])
        self.assertEqual((self.root/"policy.json").stat().st_mode & 0o777, 0o600)

    def test_expired_policy_cannot_be_reissued(self):
        with self.assertRaises(ValueError):
            packet({"policy": policy(), "context_sha256": "hash"}, session_id="session", version=2,
                   issued_ms=10000, published_ms=71000, deadline_ms=100000, gate=self.gate())

    def test_gate_consumes_time_without_renewing_one_minute_policy(self):
        out=packet({'policy':policy(),'context_sha256':'hash'},session_id='session',version=2,
                   issued_ms=10000,published_ms=18000,deadline_ms=100000,gate=self.gate())
        self.assertEqual(out['expires_ms'],70000)
        self.assertEqual(out['expires_ms']-out['published_ms'],52000)
        near_end=packet({'policy':policy(),'context_sha256':'hash'},session_id='session',version=2,
                        issued_ms=10000,published_ms=18000,deadline_ms=20000,gate=self.gate())
        self.assertEqual(near_end['expires_ms'],20000)

    def test_short_ttl_fails_future_gate(self):
        p = policy(); p["ttl_ms"] = 1000
        with self.assertRaises(ValueError):
            packet({"policy": p, "context_sha256": "hash"}, session_id="session", version=1,
                   issued_ms=10000, published_ms=12000, deadline_ms=100000, gate=self.gate())

    def test_model_and_request_size_pinned(self):
        b = self.budget(); req = request(observer(), 10000); req["model"] = "other"
        with self.assertRaises(ValueError): b.reserve(req, 10000)
        req["model"] = MODEL; req["messages"][0]["content"] = "x"*17000
        with self.assertRaises(ValueError): b.reserve(req, 10000)
        b.db.close()

    def test_config_file_duplicate_keys_rejected(self):
        path = self.root/"bad.json"; path.write_text('{"version":1,"version":2}')
        with self.assertRaises(ValueError): read_json(path)

    def test_atomic_replace_keeps_complete_new_version(self):
        p = self.root/"policy.json"; atomic(p, {"version": 1}); atomic(p, {"version": 2})
        self.assertEqual(read_json(p), {"version": 2})
        self.assertEqual(list(self.root.glob(".control-*")), [])


if __name__ == "__main__": unittest.main()
