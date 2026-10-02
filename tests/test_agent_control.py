"""Console -> named agents -> shared budget -> real engine/ABI adapters.

Venue and provider I/O is isolated. No public signing or financial transmission.
"""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_engine_execution as execution
from fastapi.testclient import TestClient
from test_engine_workspace import OWNER_TOKEN, WALLET

from economic_machine.values import MachineError
from machine_commerce.api import create_app
from machine_engine.api import create_engine_app
from machine_engine.program import example
from machine_engine.workspace import Workspace


class AgentControlTests(unittest.TestCase):
    tearDown = execution.ExecutionTests.tearDown

    def setUp(self):
        execution.ExecutionTests.setUp(self)
        self.pid2 = self.work.trading.policy(self.policy | {"name": "SECOND_STRATEGY"})["id"]
        self.shared = {
            "name": "TEAM",
            "connection_ids": [self.cid],
            "venue_policy_ids": [self.pid, self.pid2],
            "allowed_operations": ["PLAN_VENUE_ORDER", "SYNC_CONNECTION", "NATIVE_CANDIDATE"],
            "turnover_limit_usdt": "15",
            "max_order_usdt": "10",
            "max_active_runs": 4,
            "expires_at": self.at + 600,
        }
        self.team = self.work.control.policy(self.shared)["id"]
        self.alpha = self.agent("ALPHA")
        self.beta = self.agent("VAULT")

    def agent(self, name):
        return self.work.control.register(
            {
                "name": name,
                "role": "Trader",
                "policy_id": self.team,
                "connection_ids": [self.cid],
                "operations": ["PLAN_VENUE_ORDER", "SYNC_CONNECTION", "NATIVE_CANDIDATE"],
            }
        )["id"]

    def task(self, request="job-one", policy=None):
        return {
            "request_id": request,
            "operation": "PLAN_VENUE_ORDER",
            "connection_id": self.cid,
            "payload": {
                key: value
                for key, value in (self.request | {"policy_id": policy or self.pid}).items()
                if key != "request_id"
            },
        }

    def approve_and_send_fixture(self, run):
        order = self.work.trading.order(run["order_id"])
        self.work.trading.approve(order["id"], order["plan_hash"])
        return self.work.trading.dispatch(order["id"])

    def test_two_agents_and_separate_venue_policies_share_one_real_hold(self):
        first = self.work.control.run(self.alpha, self.task())
        self.assertEqual(first["status"], "AWAITING_APPROVAL")
        self.assertIsNone(self.http.submitted)
        with self.assertRaisesRegex(MachineError, "SHARED_AGENT_BUDGET"):
            self.work.control.run(self.beta, self.task("job-two", self.pid2))
        team = self.work.overview()["control"]["policies"][0]
        self.assertEqual((team["held_usdt"], team["remaining_usdt"]), ("10", "5"))
        self.assertEqual(len(self.work.overview()["control"]["agents"]), 2)

    def test_concurrent_agents_cannot_overreserve_a_shared_budget(self):
        def attempt(aid, request, pid):
            try:
                return self.work.control.run(aid, self.task(request, pid))["status"]
            except MachineError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(attempt, self.alpha, "concurrent-a", self.pid),
                pool.submit(attempt, self.beta, "concurrent-b", self.pid2),
            ]
            result = [future.result() for future in futures]
        self.assertCountEqual(result, ["AWAITING_APPROVAL", "SHARED_AGENT_BUDGET_EXCEEDED"])
        self.assertEqual(self.work.control.status()["policies"][0]["held_usdt"], "10")

    def test_run_replay_is_exact_and_keeps_agent_order_result(self):
        task = self.task()
        first = self.work.control.run(self.alpha, task)
        again = self.work.control.run(self.alpha, task)
        self.assertEqual(first, again)
        with self.assertRaisesRegex(MachineError, "IDEMPOTENCY"):
            self.work.control.run(self.beta, task)

    def test_pause_after_owner_approval_blocks_dispatch_without_releasing_hold(self):
        run = self.work.control.run(self.alpha, self.task())
        order = run["result"]
        self.work.trading.approve(order["id"], order["plan_hash"])
        self.work.control.pause_policy(self.team)
        with self.assertRaisesRegex(MachineError, "ACTIVE_SHARED_POLICY"):
            self.work.trading.dispatch(order["id"])
        self.assertIsNone(self.http.submitted)
        self.assertEqual(self.work.control.status()["policies"][0]["held_usdt"], "10")

    def test_individual_agent_pause_does_not_pause_other_agents(self):
        self.work.control.pause_agent(self.alpha)
        with self.assertRaisesRegex(MachineError, "ACTIVE_AGENT"):
            self.work.control.run(self.alpha, self.task())
        self.assertEqual(self.work.control.run(self.beta, self.task())["status"], "AWAITING_APPROVAL")

    def test_unknown_outcome_retains_shared_hold_and_restart_reconciliation_charges_once(self):
        run = self.work.control.run(self.alpha, self.task())
        self.http.failure = RuntimeError("fixture transport uncertainty")
        self.assertEqual(self.approve_and_send_fixture(run)["status"], "UNKNOWN")
        self.work = Workspace(
            self.work.runtime.db_path,
            clock=lambda: self.at,
            broker_factory=lambda connection, clock: execution.BinanceBroker(
                connection, http=self.http, clock=clock
            ),
            live_enabled=True,
        )
        self.assertEqual(self.work.control.get_run(self.alpha, run["id"])["held_usdt"], "10")
        self.http.failure = None
        self.http.result = self.http.response(self.http.submitted, "FILLED", "0.1", "9.9")
        self.work.trading.reconcile(run["order_id"])
        self.work.trading.reconcile(run["order_id"])
        policy = self.work.control.status()["policies"][0]
        self.assertEqual((policy["spent_usdt"], policy["held_usdt"]), ("9.9", "0"))
        self.assertEqual(self.work.control.get_run(self.alpha, run["id"])["charged_usdt"], "9.9")

    def test_unsent_plan_withdrawal_releases_but_unknown_plan_cannot_be_withdrawn(self):
        run = self.work.control.run(self.alpha, self.task())
        self.assertEqual(self.work.control.withdraw(self.alpha, run["id"])["held_usdt"], "0")
        with self.assertRaises(MachineError):
            self.work.trading.approve(run["order_id"], run["result"]["plan_hash"])
        new = self.work.control.run(self.beta, self.task("another"))
        self.http.failure = RuntimeError("uncertain")
        self.approve_and_send_fixture(new)
        with self.assertRaisesRegex(MachineError, "ONLY_UNSENT"):
            self.work.control.withdraw(self.beta, new["id"])
        self.assertEqual(self.work.control.status()["policies"][0]["held_usdt"], "10")

    def test_failed_plan_and_unsafe_errors_release_untransmitted_hold(self):
        self.http.balance = "0"
        run = self.work.control.run(self.alpha, self.task())
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(self.work.control.status()["policies"][0]["held_usdt"], "0")
        self.assertIsNone(self.http.submitted)

    def test_connection_and_operation_membership_cannot_be_widened_by_run_payload(self):
        other = self.work.connect(execution.SPOT)["id"]
        with self.assertRaisesRegex(MachineError, "OUTSIDE_AGENT"):
            self.work.control.run(self.alpha, self.task() | {"connection_id": other})
        narrowed = self.work.control.register(
            {
                "name": "WATCH",
                "role": "Observer",
                "policy_id": self.team,
                "connection_ids": [self.cid],
                "operations": ["SYNC_CONNECTION"],
            }
        )["id"]
        with self.assertRaisesRegex(MachineError, "OUTSIDE_AGENT"):
            self.work.control.run(narrowed, self.task())

    def test_late_plan_cannot_commit_after_expired_run_lease(self):
        original = self.work.trading.broker_factory

        def stall(connection, clock):
            broker = original(connection, clock=clock)
            original_guard = broker.guard_account

            def guarded(*args):
                result = original_guard(*args)
                self.at += 121
                self.work.control.recover_once()
                return result

            broker.guard_account = guarded
            return broker

        self.work.trading.broker_factory = stall
        run = self.work.control.run(self.alpha, self.task())
        self.assertEqual(run["status"], "INTERRUPTED")
        self.assertEqual(len(self.work.trading.status()["orders"]), 0)
        self.assertEqual(run["held_usdt"], "0")

    def test_native_cpp_run_and_sync_are_in_the_same_agent_ledger(self):
        root = Path(__file__).resolve().parents[1]
        library = root / "build/native/libmachine_kernel.so"
        import hashlib

        with patch.dict(
            os.environ,
            {
                "ENGINE_NATIVE_LIBRARY": str(library),
                "ENGINE_NATIVE_SHA256": hashlib.sha256(library.read_bytes()).hexdigest(),
            },
        ):
            work = Workspace(self.work.runtime.db_path, clock=lambda: self.at)
        task = {
            "request_id": "native-one",
            "operation": "NATIVE_CANDIDATE",
            "connection_id": self.cid,
            "payload": example(),
        }
        result = work.control.run(self.alpha, task)
        self.assertEqual(result["result"]["candidate"], "REDUCE")
        self.assertEqual(result["result"]["language_model_calls"], 0)
        self.assertEqual(result["result"]["execution_authority"], "NONE")
        task["request_id"] = "native-two"
        task["payload"]["frame"]["values"] = [900000]
        self.assertEqual(work.control.run(self.alpha, task)["status"], "ABSTAINED")
        snapshot = {
            "observed_at": self.at,
            "source_hash": "a" * 64,
            "assets": [],
            "positions": [],
            "orders": [],
        }
        with patch.object(work.readers, "read", return_value=snapshot):
            synced = work.control.run(
                self.beta,
                {
                    "request_id": "sync-one",
                    "operation": "SYNC_CONNECTION",
                    "connection_id": self.cid,
                    "payload": {},
                },
            )
        self.assertEqual(synced["status"], "SUCCEEDED")
        self.assertEqual(work.overview()["connections"][0]["observed_at"], self.at)
        self.assertEqual(len(work.overview()["control"]["runs"]), 3)

    def test_local_keys_are_hashed_bound_and_revoked_without_owner_authority(self):
        app = create_engine_app(
            self.work.runtime.db_path, admin_token=OWNER_TOKEN, workspace=self.work, clock=lambda: self.at
        )
        owner = TestClient(app)
        owner.headers["Authorization"] = "Bearer " + OWNER_TOKEN
        issued = owner.post(f"/api/engine/agents/{self.alpha}/keys", json={"ttl_seconds": 300}).json()
        agent = TestClient(app)
        agent.headers["Authorization"] = "Bearer " + issued["secret"]
        self.assertEqual(agent.get(f"/api/engine/agents/{self.alpha}").status_code, 200)
        self.assertEqual(agent.get(f"/api/engine/agents/{self.beta}").status_code, 403)
        self.assertEqual(agent.post("/api/engine/control-policies", json={}).status_code, 403)
        self.assertEqual(
            agent.post(f"/api/engine/agents/{self.alpha}/keys", json={"ttl_seconds": 300}).status_code, 403
        )
        run = agent.post(f"/api/engine/agents/{self.alpha}/runs", json=self.task())
        self.assertEqual(run.status_code, 200, run.text)
        self.assertIsNone(self.http.submitted)
        self.assertEqual(
            agent.post(f"/api/engine/trade/orders/{run.json()['order_id']}/approve", json={}).status_code, 403
        )
        self.assertNotIn(issued["secret"], json.dumps(self.work.overview()))
        with self.work.runtime.connect() as db:
            self.assertNotIn(
                issued["secret"], str([tuple(row) for row in db.execute("SELECT * FROM engine_agent_keys")])
            )
        owner.post(f"/api/engine/agent-keys/{issued['key']['id']}/revoke", json={})
        self.assertEqual(agent.get(f"/api/engine/agents/{self.alpha}").status_code, 401)

    def test_hosted_agent_key_binding_uses_owner_workspace_and_denies_cross_agent_runs(self):
        with patch("machine_engine.workspace.Connectors") as readers:
            readers.return_value.read.return_value = {
                "observed_at": self.at,
                "source_hash": "b" * 64,
                "assets": [],
                "positions": [],
                "orders": [],
            }
            app = create_app(Path(self.tmp.name) / "hosted.sqlite3", clock=lambda: self.at)
            owner, client = TestClient(app), TestClient(app)
            owner.post("/api/sessions", json={})
            cid = owner.post("/api/engine/connections", json=WALLET).json()["id"]
            team = owner.post(
                "/api/engine/control-policies",
                json=self.shared
                | {
                    "connection_ids": [cid],
                    "venue_policy_ids": [],
                    "allowed_operations": ["SYNC_CONNECTION"],
                    "turnover_limit_usdt": "0",
                    "max_order_usdt": "0",
                },
            ).json()["id"]
            body = {
                "name": "WATCH",
                "role": "Analyst",
                "policy_id": team,
                "connection_ids": [cid],
                "operations": ["SYNC_CONNECTION"],
            }
            aid = owner.post("/api/engine/agents", json=body).json()["id"]
            other = owner.post("/api/engine/agents", json=body | {"name": "VAULT"}).json()["id"]
            key = owner.post(
                "/api/keys",
                json={
                    "name": "WATCH key",
                    "scopes": ["agents:run"],
                    "policy_id": None,
                    "ttl_seconds": 300,
                    "engine_agent_id": aid,
                },
            ).json()
            client.headers["Authorization"] = "Bearer " + key["secret"]
            raw = {
                "request_id": "watch-job",
                "operation": "SYNC_CONNECTION",
                "connection_id": cid,
                "payload": {},
            }
            run = client.post(f"/api/engine/agents/{aid}/runs", json=raw)
            self.assertEqual(run.status_code, 200, run.text)
            self.assertEqual(run.json()["status"], "SUCCEEDED")
            self.assertEqual(client.post(f"/api/engine/agents/{other}/runs", json=raw).status_code, 403)
            self.assertEqual(client.post("/api/engine/trade/orders", json={}).status_code, 403)
            self.assertEqual(client.post(f"/api/engine/agents/{aid}/pause", json={}).status_code, 403)
            self.assertEqual(
                owner.post(
                    "/api/keys",
                    json={
                        "name": "bad",
                        "scopes": ["agents:run", "engine:write"],
                        "policy_id": None,
                        "ttl_seconds": 300,
                        "engine_agent_id": aid,
                    },
                ).status_code,
                409,
            )
            owner.post(f"/api/keys/{key['key']['id']}/revoke", json={})
            self.assertEqual(client.post(f"/api/engine/agents/{aid}/runs", json=raw).status_code, 401)

    def test_shared_policy_expiry_blocks_a_previously_approved_order(self):
        run = self.work.control.run(self.alpha, self.task())
        self.work.trading.approve(run["order_id"], run["result"]["plan_hash"])
        self.at += 601
        with self.assertRaisesRegex(MachineError, "ACTIVE_SHARED_POLICY"):
            self.work.trading.dispatch(run["order_id"])
        self.assertIsNone(self.http.submitted)
        team = self.work.control.status()["policies"][0]
        self.assertEqual((team["status"], team["held_usdt"]), ("EXPIRED", "10"))

    def test_observed_overshoot_breaches_team_and_cannot_be_hidden_by_replay(self):
        team = self.work.control.policy(self.shared | {"name": "TIGHT", "turnover_limit_usdt": "10"})["id"]
        aid = self.work.control.register(
            {
                "name": "TIGHT_AGENT",
                "role": "Trader",
                "policy_id": team,
                "connection_ids": [self.cid],
                "operations": ["PLAN_VENUE_ORDER"],
            }
        )["id"]
        task = self.task()
        task["payload"]["side"] = "SELL"
        run = self.work.control.run(aid, task)
        self.work.trading.approve(run["order_id"], run["result"]["plan_hash"])
        self.work.trading.dispatch(run["order_id"])
        self.http.result = self.http.response(self.http.submitted, "FILLED", "0.1", "10.5")
        self.work.trading.reconcile(run["order_id"])
        team = next(p for p in self.work.control.status()["policies"] if p["id"] == team)
        self.assertEqual(
            (team["status"], team["spent_usdt"], team["remaining_usdt"]), ("BREACHED", "10.5", "0")
        )
        with self.assertRaisesRegex(MachineError, "ACTIVE_SHARED_POLICY"):
            self.work.control.run(aid, self.task("after-breach"))

    def test_shared_task_concurrency_limit_applies_before_adapter_work(self):
        with self.work.runtime.connect() as db:
            body = json.loads(
                db.execute("SELECT body FROM engine_control_policies WHERE id=?", (self.team,)).fetchone()[0]
            )
            body["max_active_runs"] = 1
            db.execute("UPDATE engine_control_policies SET body=? WHERE id=?", (json.dumps(body), self.team))
        from threading import Event

        entered, release = Event(), Event()

        def pending(_cid):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture deadlock")
            return {"new_snapshot": True, "error_code": None}

        with patch.object(self.work, "sync", pending), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self.work.control.run,
                self.alpha,
                {
                    "request_id": "slow-sync",
                    "operation": "SYNC_CONNECTION",
                    "connection_id": self.cid,
                    "payload": {},
                },
            )
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaisesRegex(MachineError, "SHARED_ACTIVE_RUN_LIMIT"):
                    self.work.control.run(self.beta, self.task())
            finally:
                release.set()
            self.assertEqual(future.result()["status"], "SUCCEEDED")

    def test_expired_plan_has_one_status_in_agent_and_execution_views(self):
        task = self.task()
        run = self.work.control.run(self.alpha, task)
        self.at += 61
        order = self.work.trading.order(run["order_id"])
        replay = self.work.control.run(self.alpha, task)
        control = self.work.overview()["control"]["runs"][0]
        self.assertEqual((order["status"], replay["status"], control["status"]), ("EXPIRED_UNSENT",) * 3)
        self.assertEqual(replay["result"], order)
        self.assertEqual(control["held_usdt"], "10")
        self.assertEqual(self.work.control.withdraw(self.alpha, run["id"])["held_usdt"], "0")
