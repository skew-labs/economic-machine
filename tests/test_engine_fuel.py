import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_gas_router import TEST, FakeNetwork, sign

from economic_machine.values import MachineError
from machine_commerce.access import Access, Principal
from machine_commerce.api import create_app
from machine_commerce.gas_router import GasRouter
from machine_engine.connections import Connectors, normalize_connection
from machine_engine.fuel import Fuel
from machine_engine.workspace import Workspace


class EngineFuelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.net = FakeNetwork()
        self.router = GasRouter(self.net.read, self.net.api, lambda: self.net.now)
        self.work = Workspace(Path(self.tmp.name)/"workspace.sqlite3", clock=lambda: self.net.now)
        self.cid = self.work.connect({"name": "Wallet", "profile": "arbitrum-one-wallet", "config": {"address": TEST.address}})["id"]
        pid = self.work.control.policy({"name": "Agents", "connection_ids": [self.cid], "venue_policy_ids": [],
            "allowed_operations": ["SYNC_CONNECTION"], "turnover_limit_usdt": "0", "max_order_usdt": "0",
            "max_active_runs": 2, "expires_at": self.net.now+3600})["id"]
        self.aids = [self.work.control.register({"name": name, "role": "Buyer", "policy_id": pid,
                     "connection_ids": [self.cid], "operations": ["SYNC_CONNECTION"]})["id"] for name in ("ALPHA", "VAULT")]
        self.fuel = Fuel(self.work, self.router)
        self.policy = self.fuel.policy({"owner": TEST.address, "agent_ids": self.aids, "budget_atoms": 3000000,
            "max_fuel_atoms": 2000000, "max_purchase_atoms": 1000000, "expires_at": self.net.now+3600})["id"]
        self.request = {"request_id": "job-one", "policy_id": self.policy, "agent_id": self.aids[0],
            "parent_action_hash": "ab"*32, "purchase_atoms": 1000000, "fuel_atoms": 2000000,
            "required_eth_wei": 500000000000000}

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_usdc_budget_covers_both_agents_and_parent_purchase(self):
        first = self.fuel.propose(self.request)
        self.assertEqual(first["held_atoms"], 3000000)
        self.assertEqual(self.fuel.propose(self.request)["id"], first["id"])
        with self.assertRaisesRegex(MachineError, "BUDGET_EXHAUSTED"):
            self.fuel.propose({**self.request, "request_id": "job-two", "agent_id": self.aids[1]})
        with self.assertRaisesRegex(MachineError, "IDEMPOTENCY"):
            self.fuel.propose({**self.request, "parent_action_hash": "cd"*32})

    def test_owner_can_set_larger_policy_but_cannot_overspend_it(self):
        policy=self.fuel.policy({'owner':TEST.address,'agent_ids':self.aids,'budget_atoms':100000000,
            'max_fuel_atoms':20000000,'max_purchase_atoms':80000000,'expires_at':self.net.now+3600})
        proposal={**self.request,'policy_id':policy['id'],'fuel_atoms':10000000,'purchase_atoms':1000000}
        self.assertEqual(self.fuel.propose(proposal)['held_atoms'],11000000)
        with self.assertRaisesRegex(MachineError,'POLICY_AMOUNT_EXCEEDED'):
            self.fuel.propose({**proposal,'request_id':'larger','fuel_atoms':20000001})

    def test_mainnet_connector_pins_chain_token_and_finalized_height(self):
        from test_engine_workspace import FakeHTTP
        http = FakeHTTP()
        original = http.request
        def read(url, **options):
            result = original(url, **options)
            if options["body"]["method"] == "eth_chainId": result["result"] = hex(42161)
            return result
        http.request = read
        connection = normalize_connection({"name": "Mainnet", "profile": "arbitrum-one-wallet", "config": {"address": TEST.address}})
        result = Connectors(http).read(connection)
        self.assertEqual(result["network"], "eip155:42161")
        self.assertEqual(http.calls[-1][1]["body"]["params"][0]["to"], "0xaf88d065e77c8cc2239327c5edb3a432268e5831")
        self.assertTrue(all(url == "https://arb1.arbitrum.io/rpc" for url, _ in http.calls))
        self.assertEqual(http.calls[-1][1]["body"]["params"][1], hex(123))

    def test_wallet_inventory_includes_other_policies_parent_reservations(self):
        first = self.fuel.propose(self.request)
        with self.work.runtime.connect() as db:
            db.execute("UPDATE swaps SET state='FILLED_FINALIZED' WHERE id=?", (first["swap"]["id"],))
            db.execute("UPDATE engine_fuel_requests SET held_atoms=25000000 WHERE id=?", (first["id"],))
        policy = self.fuel.policy({"owner": TEST.address, "agent_ids": self.aids, "budget_atoms": 3000000,
            "max_fuel_atoms": 2000000, "max_purchase_atoms": 1000000, "expires_at": self.net.now+3600})["id"]
        with self.assertRaisesRegex(MachineError, "ALREADY_RESERVED"):
            self.fuel.propose({**self.request, "request_id": "other-policy", "policy_id": policy})

    def test_concurrent_requests_cannot_overreserve_after_slow_quote(self):
        def propose(i):
            try:
                return Fuel(self.work, self.router).propose({**self.request, "request_id": "parallel-"+str(i), "agent_id": self.aids[i]})["status"]
            except MachineError as exc: return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(propose, (0, 1)))
        self.assertCountEqual(results, ["AWAITING_WALLET", "SHARED_FUEL_BUDGET_EXHAUSTED"])

    def test_chain_receipt_is_required_and_charged_exactly_once(self):
        p = self.fuel.propose(self.request)
        with self.assertRaises(MachineError): self.fuel.resume_review(p["id"], self.request["parent_action_hash"])
        o = self.fuel.wallet_step(p["id"], "order", sign(p["swap"]["permit"]))
        self.net.intent = o
        self.fuel.wallet_step(p["id"], "submit", sign(o["order"]))
        pending = self.fuel.reconcile(p["id"])
        self.assertEqual(pending["charged_atoms"], 0)
        self.assertEqual(pending["held_atoms"], 3000000)
        self.net.provider_status = "fulfilled"
        done = self.fuel.reconcile(p["id"])
        self.assertEqual((done["status"], done["charged_atoms"], done["held_atoms"]), ("PARENT_REPLAN_REQUIRED", 2000000, 1000000))
        restarted = Fuel(self.work, self.router)
        restarted.reconcile(p["id"])
        with self.work.runtime.connect() as db:
            self.assertEqual(db.execute("SELECT spent_atoms FROM engine_fuel_policies WHERE id=?", (self.policy,)).fetchone()[0], 2000000)
        with self.assertRaisesRegex(MachineError, "BALANCE_CHANGED"): restarted.resume_review(p["id"], self.request["parent_action_hash"])
        original = self.router.reader
        self.router.reader = lambda url, method, params: hex(10**15) if method == "eth_getBalance" else original(url, method, params)
        with self.assertRaises(MachineError): restarted.resume_review(p["id"], "cd"*32)
        ready = restarted.resume_review(p["id"], self.request["parent_action_hash"])
        self.assertEqual(ready["execution_authority"], "NONE")
        self.assertTrue(ready["requires_fresh_parent_simulation"])
        with self.assertRaises(MachineError): restarted.resume_review(p["id"], self.request["parent_action_hash"])
        with self.assertRaises(MachineError): restarted.cancel_parent(p["id"])

    def test_pause_blocks_signing_but_retains_ambiguous_reservation(self):
        p = self.fuel.propose(self.request)
        self.fuel.pause(self.policy)
        with self.assertRaisesRegex(MachineError, "ACTIVE_FUEL_POLICY"):
            self.fuel.wallet_step(p["id"], "order", sign(p["swap"]["permit"]))
        self.assertEqual(self.fuel.get(p["id"])["held_atoms"], 3000000)

    def test_disconnected_wallet_and_unapproved_agent_cannot_use_policy(self):
        self.work.disconnect(self.cid)
        with self.assertRaisesRegex(MachineError, "CONNECTED_ARBITRUM_WALLET"):
            self.fuel.propose(self.request)

    def test_expired_unsigned_proposal_releases_both_holds(self):
        p = self.fuel.propose(self.request)
        self.net.now += 601
        result = self.fuel.reconcile(p["id"])
        self.assertEqual(result["status"], "EXPIRED_UNFILLED")
        self.assertEqual(result["held_atoms"], 0)
        self.assertEqual(result["charged_atoms"], 0)

    def test_agent_key_can_propose_and_reconcile_but_cannot_authorize_or_raise_limits(self):
        writer = Principal("session", "key", frozenset({"engine:write", "engine:read"}))
        for method, path in [("POST", "/api/engine/fuel/requests"), ("GET", "/api/engine/fuel/requests/fuel-1"),
                             ("POST", "/api/engine/fuel/requests/fuel-1/reconcile")]:
            Access.authorize(writer, method, path)
        for path in ["/api/engine/fuel/policies", "/api/engine/fuel/requests/fuel-1/submit", "/api/engine/fuel/requests/fuel-1/order",
                     "/api/engine/fuel/requests/fuel-1/resume-review", "/api/engine/fuel/requests/fuel-1/cancel-parent"]:
            with self.subTest(path=path), self.assertRaises(PermissionError): Access.authorize(writer, "POST", path)

    def test_hosted_http_agent_proposal_owner_signature_and_receipt_connection(self):
        app = create_app(Path(self.tmp.name)/"commerce.sqlite3", lambda: self.net.now)
        owner, agent, guest = TestClient(app), TestClient(app), TestClient(app)
        try:
            self.assertEqual(owner.post("/api/sessions", json={}).status_code, 200)
            key = owner.post("/api/keys", json={"name": "Fuel agent", "scopes": ["engine:write", "engine:read"],
                             "policy_id": None, "ttl_seconds": 3600})
            self.assertEqual(key.status_code, 200, key.text)
            agent.headers["Authorization"] = "Bearer " + key.json()["secret"]
            with patch("machine_engine.fuel_routes.Fuel", return_value=self.fuel):
                proposed = agent.post("/api/engine/fuel/requests", json=self.request)
                self.assertEqual(proposed.status_code, 200, proposed.text)
                p = proposed.json(); path = "/api/engine/fuel/requests/" + p["id"]
                self.assertEqual(p["wallet_review_url"], "/commerce/console?fuel=" + p["id"] + "#overview")
                self.assertEqual(guest.get(path).status_code, 401)
                self.assertEqual(agent.get(path).status_code, 200)
                for suffix in ["order", "submit", "resume-review", "cancel-parent"]:
                    self.assertEqual(agent.post(path+"/"+suffix, json={}).status_code, 403)
                order = owner.post(path+"/order", json={"signature": sign(p["swap"]["permit"])})
                self.assertEqual(order.status_code, 200, order.text)
                self.net.intent = order.json()
                submitted = owner.post(path+"/submit", json={"signature": sign(order.json()["order"])})
                self.assertEqual(submitted.json()["status"], "SUBMITTED")
                self.net.provider_status = "fulfilled"
                done = agent.post(path+"/reconcile", json={})
                self.assertEqual(done.json()["status"], "PARENT_REPLAN_REQUIRED")
                self.assertEqual(done.json()["charged_atoms"], 2000000)
                self.assertEqual(self.net.posts, 1)
        finally:
            owner.close(); agent.close(); guest.close()


if __name__ == "__main__": unittest.main()
