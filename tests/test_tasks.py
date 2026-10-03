"""Durable practical work; no inferred context, authority, or external purchases."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from test_engine_workspace import OWNER_TOKEN, WALLET

from economic_machine.journal import verify_journal
from economic_machine.values import MachineError
from machine_commerce.api import create_app
from machine_engine.api import create_engine_app
from machine_engine.tasks import KINDS, TaskServices
from machine_engine.workspace import Workspace


def brief(key="create-one", **changes):
    return {
        "request_id": key,
        "kind": "vendor_comparison",
        "title": "CRM vendor shortlist",
        "instructions": "Compare total cost, support and deployment for our procurement meeting.",
        "budget": {"currency": "USD", "maximum": "10"},
        "deadline_at": None,
        "constraints": {
            "output_language": "en",
            "output_format": "table",
            "comparison_fields": ["Price", "Support"],
            "regions": ["SG", "JP"],
        },
        "preference_id": None,
        "connection_ids": [],
    } | changes


class TasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "work.db"
        self.at = 1790900000
        self.work = Workspace(self.path, clock=lambda: self.at)

    def tearDown(self):
        self.tmp.cleanup()

    def remember(self, task, key="remember-one", fields=None, name="Procurement"):
        return self.work.tasks.remember(
            task["id"],
            {
                "request_id": key,
                "expected_revision": task["revision"],
                "name": name,
                "fields": fields or ["regions", "output_language", "output_format", "comparison_fields"],
                "expires_at": self.at + 600,
            },
        )

    def test_five_real_work_categories_with_typed_deliverables(self):
        for kind, template in KINDS.items():
            conditions = {
                "output_language": "ko",
                "source_language": "en",
                "output_format": template["formats"][0],
                "comparison_fields": ["Support"],
                "required_fields": ["email"],
            }
            task = self.work.tasks.create(brief(kind, kind=kind, constraints=conditions))
            self.assertEqual(task["status"], "READY_TO_PLAN")
            self.assertEqual(task["brief"]["budget"]["cents"], 1000)
            self.assertEqual(task["payment_authority"], "NONE")
            self.assertFalse(task["brief"]["automatic_execution"])

    def test_budget_is_exact_usd_and_is_not_inherited(self):
        first = self.work.tasks.create(brief())
        self.remember(first)
        next_task = self.work.tasks.create(
            brief("next", preference_id="last", constraints={}, budget={"currency": "USD", "maximum": "2.01"})
        )
        self.assertEqual(next_task["brief"]["budget"]["cents"], 201)
        for raw in [
            {"currency": "USDC", "maximum": "10"},
            {"currency": "USD", "maximum": 10},
            {"currency": "USD", "maximum": "1e2"},
            {"currency": "USD", "maximum": "10000.01"},
            {"currency": "USD", "maximum": "0.001"},
        ]:
            with self.subTest(raw=raw), self.assertRaises(MachineError):
                self.work.tasks.create(brief("bad", budget=raw))
        self.assertEqual(
            self.work.tasks.create(brief("free", budget={"currency": "USD", "maximum": "0"}))["brief"][
                "budget"
            ]["cents"],
            0,
        )

    def test_last_context_missing_does_not_fabricate_regions(self):
        task = self.work.tasks.create(brief(preference_id="last", constraints={}))
        self.assertEqual(task["status"], "NEEDS_INPUT")
        self.assertIn("confirmed_preferences", task["missing_information"])
        self.assertNotIn("regions", task["brief"]["constraints"])
        with self.assertRaisesRegex(MachineError, "RESOLVED_TASK"):
            self.remember(task)

    def test_explicit_override_and_pinned_preference_version(self):
        first = self.work.tasks.create(brief())
        saved = self.remember(first)
        second = self.work.tasks.create(brief("next", constraints={"regions": ["KR"]}, preference_id="last"))
        self.assertEqual(second["brief"]["constraints"]["regions"], ["KR"])
        self.assertEqual(second["brief"]["inherited_preference"]["id"], saved["id"])
        self.assertNotIn("regions", second["brief"]["inherited_fields"])
        newer = self.remember(first, key="remember-new")
        self.assertEqual(newer["version"], 2)
        self.assertEqual(self.work.tasks.get(second["id"])["brief"]["inherited_preference"]["version"], 1)

    def test_revoke_invalidates_inherited_context_not_explicit_overrides(self):
        first = self.work.tasks.create(brief())
        saved = self.remember(first)
        inherited = self.work.tasks.create(brief("inherited", preference_id=saved["id"], constraints={}))
        explicit = self.work.tasks.create(brief("explicit", preference_id=saved["id"]))
        self.work.tasks.revoke(saved["id"])
        self.assertEqual(self.work.tasks.get(inherited["id"])["status"], "NEEDS_INPUT")
        self.assertEqual(self.work.tasks.get(explicit["id"])["status"], "READY_TO_PLAN")
        self.assertEqual(self.work.tasks.revoke(saved["id"])["status"], "REVOKED")

    def test_preferences_are_scoped_by_work_type(self):
        self.remember(self.work.tasks.create(brief()))
        task = self.work.tasks.create(
            brief("research", kind="research_brief", preference_id="last", constraints={})
        )
        self.assertEqual(task["status"], "NEEDS_INPUT")

    def test_preference_expiry_and_task_deadline_are_live_read_checks(self):
        first = self.work.tasks.create(brief(deadline_at=self.at + 1000))
        self.remember(first)
        derived = self.work.tasks.create(brief("derived", preference_id="last", constraints={}))
        self.at += 601
        self.assertEqual(self.work.tasks.get(derived["id"])["status"], "NEEDS_INPUT")
        self.assertEqual(self.work.tasks.status()["preferences"][0]["status"], "EXPIRED")
        self.at += 400
        self.assertEqual(self.work.tasks.get(first["id"])["status"], "EXPIRED")

    def test_no_payment_credentials_or_permissions_can_be_remembered(self):
        first = self.work.tasks.create(brief())
        for field in ["budget", "permissions", "payment_authority", "connection_ids", "approval"]:
            with self.subTest(field=field), self.assertRaises(MachineError):
                self.remember(first, fields=[field])
            with self.assertRaises(MachineError):
                self.work.tasks.create(brief("bad", constraints={field: "yes"}))
        self.assertEqual(self.work.tasks.status()["preferences"], [])

    def test_invalid_fields_and_common_credentials_rejected_without_journal(self):
        for raw in [
            brief(kind=[]),
            brief(instructions="sk-bk-" + "a" * 48),
            brief(title=" "),
            brief(constraints={"regions": ["SG", "SG"]}),
            brief(constraints={"output_format": "csv"}),
            brief(deadline_at=self.at),
            brief(deadline_at=True),
            brief(unexpected="bad"),
        ]:
            with self.subTest(raw=raw), self.assertRaises(MachineError):
                self.work.tasks.create(raw)
        self.assertEqual(self.work.tasks.status()["tasks"], [])
        with self.work.runtime.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM engine_task_requests").fetchone()[0], 0)

    def test_request_replay_is_exact_conflicting_key_rejected(self):
        first = self.work.tasks.create(brief())
        self.assertEqual(self.work.tasks.create(brief()), first)
        with self.assertRaisesRegex(MachineError, "REUSED"):
            self.work.tasks.create(brief(title="Changed"))
        self.assertEqual(len(self.work.tasks.status()["tasks"]), 1)

    def test_concurrent_retries_create_one_task(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.work.tasks.create(brief()), range(4)))
        self.assertTrue(all(t == values[0] for t in values))
        self.assertEqual(len(self.work.tasks.status()["tasks"]), 1)

    def test_immutable_revisions_and_optimistic_concurrent_edits(self):
        first = self.work.tasks.create(brief())
        raw = {k: v for k, v in brief(title="New title").items() if k != "request_id"}

        def edit(key):
            try:
                return self.work.tasks.revise(
                    first["id"], {"request_id": key, "expected_revision": 1, "draft": raw}
                )["revision"]
            except MachineError as e:
                return str(e)

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(edit, ["edit-one", "edit-two"]))
        self.assertCountEqual(values, [2, "TASK_CHANGED_REFRESH_BEFORE_EDITING"])
        result = self.work.tasks.get(first["id"])
        self.assertEqual(len(result["history"]), 2)
        self.assertEqual(result["history"][0]["body_hash"], first["brief_hash"])
        self.assertNotEqual(result["history"][1]["body_hash"], first["brief_hash"])

    def test_cancel_is_idempotent_unsent_and_prevents_edits(self):
        first = self.work.tasks.create(brief())
        raw = {"request_id": "cancel", "expected_revision": 1}
        result = self.work.tasks.cancel(first["id"], raw)
        self.assertEqual(result["status"], "CANCELLED_UNSENT")
        self.assertEqual(self.work.tasks.cancel(first["id"], raw), result)
        with self.assertRaises(MachineError):
            self.work.tasks.revise(
                first["id"],
                {
                    "request_id": "edit",
                    "expected_revision": 1,
                    "draft": {k: v for k, v in brief().items() if k != "request_id"},
                },
            )
        self.assertEqual(result["payment_authority"], "NONE")

    def test_restart_preserves_tasks_preferences_and_integrity(self):
        first = self.work.tasks.create(brief())
        saved = self.remember(first)
        restarted = Workspace(self.path, clock=lambda: self.at)
        self.assertEqual(restarted.tasks.get(first["id"])["brief_hash"], first["brief_hash"])
        self.assertEqual(restarted.tasks.status()["preferences"][0]["id"], saved["id"])
        with restarted.runtime.connect() as db:
            self.assertTrue(verify_journal(db))

    def test_connections_must_belong_to_workspace_and_remain_active(self):
        cid = self.work.connect(WALLET)["id"]
        first = self.work.tasks.create(brief(connection_ids=[cid]))
        self.work.disconnect(cid)
        self.assertEqual(self.work.tasks.get(first["id"])["status"], "NEEDS_INPUT")
        other = Workspace(Path(self.tmp.name) / "other.db", clock=lambda: self.at)
        with self.assertRaises(MachineError):
            other.tasks.create(brief(connection_ids=[cid]))

    def test_modified_revision_body_fails_integrity_check(self):
        task = self.work.tasks.create(brief())
        with self.work.runtime.connect() as db:
            row = db.execute("SELECT body FROM engine_task_revisions").fetchone()
            body = json.loads(row[0])
            body["budget"]["cents"] = 1
            db.execute("UPDATE engine_task_revisions SET body=?", (json.dumps(body),))
        with self.assertRaisesRegex(MachineError, "INTEGRITY_FAILED"):
            self.work.tasks.get(task["id"])

    def test_modified_preference_body_fails_integrity_check(self):
        self.remember(self.work.tasks.create(brief()))
        with self.work.runtime.connect() as db:
            db.execute("UPDATE engine_task_preferences SET body=?", ('{"regions":["US"]}',))
        with self.assertRaisesRegex(MachineError, "INTEGRITY_FAILED"):
            self.work.tasks.create(brief("derived", preference_id="last", constraints={}))

    def test_catalogue_has_no_invented_merchants_prices_or_purchase_authority(self):
        self.assertEqual(TaskServices().catalogue()["services"], [])
        raw = {
            "sku": "research-v1",
            "merchant_id": "operator-vendor",
            "name": "Brief",
            "kind": "research_brief",
            "price": {"currency": "USD", "amount": "2.50"},
            "version": 1,
        }
        catalog = TaskServices([raw]).catalogue()
        self.assertEqual(catalog["services"][0]["price"]["cents"], 250)
        self.assertFalse(catalog["services"][0]["purchase_enabled"])
        self.assertFalse(catalog["services"][0]["fulfillment_connected"])
        for values in [
            [raw, raw],
            [raw | {"version": True}],
            [raw | {"kind": []}],
            [raw | {"price": {"currency": "USDC", "amount": "2.50"}}],
        ]:
            with self.assertRaises(MachineError):
                TaskServices(values)


class TaskRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.tmp.name) / "commerce.db", lambda: 1790900000)
        self.owner = TestClient(self.app)
        self.owner.post("/api/sessions", json={})
        self.other = TestClient(self.app)
        self.other.post("/api/sessions", json={})

    def tearDown(self):
        self.owner.close()
        self.other.close()
        self.tmp.cleanup()

    def test_hosted_owner_tasks_overview_and_foreign_read_memory_are_isolated(self):
        first = self.owner.post("/api/engine/tasks", json=brief())
        self.assertEqual(first.status_code, 200, first.text)
        tid = first.json()["id"]
        self.assertEqual(self.owner.get("/api/engine/overview").json()["tasks"]["tasks"][0]["id"], tid)
        self.assertEqual(self.other.get("/api/engine/tasks").json()["tasks"], [])
        self.assertEqual(self.other.get("/api/engine/tasks/" + tid).status_code, 409)
        saved = self.owner.post(
            "/api/engine/tasks/" + tid + "/remember",
            json={
                "request_id": "remember",
                "expected_revision": 1,
                "name": "CRM",
                "fields": ["regions"],
                "expires_at": 1790900600,
            },
        ).json()
        wrong = self.other.post(
            "/api/engine/tasks", json=brief("foreign", preference_id=saved["id"], constraints={})
        )
        self.assertEqual(wrong.json()["status"], "NEEDS_INPUT")
        self.assertEqual(
            self.other.post("/api/engine/task-preferences/" + saved["id"] + "/revoke", json={}).status_code,
            409,
        )

    def test_agent_writer_can_draft_but_cannot_confirm_cancel_or_remember(self):
        created = self.owner.post(
            "/api/keys",
            json={
                "name": "Task assistant",
                "scopes": ["engine:write", "engine:read"],
                "policy_id": None,
                "ttl_seconds": 3600,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        headers = {"Authorization": "Bearer " + created.json()["secret"]}
        first = self.owner.post("/api/engine/tasks", json=brief(), headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        tid = first.json()["id"]
        self.assertEqual(self.owner.get("/api/engine/tasks/" + tid, headers=headers).status_code, 200)
        for path in [
            f"/api/engine/tasks/{tid}/remember",
            f"/api/engine/tasks/{tid}/cancel",
            "/api/engine/task-preferences/foreign/revoke",
        ]:
            self.assertEqual(self.owner.post(path, json={}, headers=headers).status_code, 403)
        self.assertEqual(self.owner.post("/api/payments", json={}, headers=headers).status_code, 403)

    def test_read_key_cannot_create_task_and_missing_auth_fails(self):
        key = self.owner.post(
            "/api/keys",
            json={"name": "Reader", "scopes": ["engine:read"], "policy_id": None, "ttl_seconds": 3600},
        ).json()
        self.assertEqual(
            self.owner.post(
                "/api/engine/tasks", json=brief(), headers={"Authorization": "Bearer " + key["secret"]}
            ).status_code,
            403,
        )
        guest = TestClient(self.app)
        self.assertEqual(guest.get("/api/engine/tasks").status_code, 401)
        guest.close()

    def test_local_owner_console_uses_same_task_routes_and_requires_token(self):
        app = create_engine_app(Path(self.tmp.name) / "local.db", admin_token=OWNER_TOKEN)
        client = TestClient(app)
        self.assertEqual(client.get("/api/engine/tasks").status_code, 401)
        client.headers["Authorization"] = "Bearer " + OWNER_TOKEN
        task = client.post("/api/engine/tasks", json=brief())
        self.assertEqual(task.status_code, 200, task.text)
        self.assertEqual(client.get("/api/engine/tasks").json()["tasks"][0]["id"], task.json()["id"])
        self.assertEqual(client.get("/tasks.js").status_code, 200)
        self.assertEqual(client.get("/tasks.css").status_code, 200)
        client.close()

    def test_hosted_and_local_assets_serve_task_ui_without_replacement_console(self):
        html = self.owner.get("/").text
        self.assertIn('id="tasks-view"', html)
        for asset in ["/tasks.js", "/tasks.css"]:
            self.assertEqual(self.owner.get(asset).status_code, 200)


if __name__ == "__main__":
    unittest.main()
