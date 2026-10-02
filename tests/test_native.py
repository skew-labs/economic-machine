import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from economic_machine.values import MachineError
from machine_engine.native import NativeGate
from machine_engine.program import NativeProgram
from machine_commerce.api import create_app
from machine_commerce.portal import create_portal
from machine_engine.program import example


def instruction(op, dst, unit, value=0, a=0, b=0, c=0):
    return dict(op=op, dst=dst, unit=unit, value=value, a=a, b=b, c=c)


def program():
    return dict(version=1, fields=["rate"], instructions=[
        instruction("load", 0, "rate"), instruction("constant", 1, "rate", 800000),
        instruction("less", 2, "boolean", a=0, b=1), instruction("assert_true", 3, "boolean", a=2),
        instruction("constant", 4, "scalar", 900000), instruction("candidate", 5, "scalar", 2, a=4)])


def frame():
    return dict(version=1, valid=1, sequence=7, expected_sequence=7, observed_ns=900,
                now_ns=950, max_age_ns=100, deadline_ns=1100, values=[700000])


def candidate():
    return dict(version=1, side=1, sequence=7, expected_sequence=7, now_ns=1000, observed_ns=950,
        max_age_ns=100, deadline_ns=1100, quantity=1000000, price=10000000, reference_price=10000000,
        quantity_step=1000, price_tick=1000, min_notional=1000000, max_order=20000000,
        available_quote=30000000, available_base=2000000, exposure_after=10000000,
        max_exposure=20000000, turnover_remaining=30000000, max_slippage_bps=15,
        fee_reserve_bps=20, policy_active=1, oracle_valid=1)


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(__file__).resolve().parents[1] / "build/native/libmachine_kernel.so"
        if not cls.path.is_file():
            raise RuntimeError("Compile native library on the remote host before this suite; no silent skip")
        cls.checksum = hashlib.sha256(cls.path.read_bytes()).hexdigest()

    def setUp(self):
        self.gate = NativeGate(str(self.path), self.checksum)
        self.vm = NativeProgram(self.gate)

    def test_hash_pinning_and_symlink_reject_unsafe_library(self):
        with self.assertRaisesRegex(MachineError, "HASH_MISMATCH"): NativeGate(str(self.path), "0" * 64)
        with tempfile.TemporaryDirectory() as folder:
            link = Path(folder) / "lib.so"; link.symlink_to(self.path)
            with self.assertRaisesRegex(MachineError, "TRUSTED_NATIVE"): NativeGate(str(link), self.checksum)

    def test_c_layout_and_microamounts_cross_the_real_abi(self):
        result = self.gate.evaluate(candidate())
        self.assertTrue(result["accepted"])
        self.assertEqual(result["required_quote"], 10020000)
        self.assertEqual(result["execution_authority"], 0)
        with self.assertRaises(MachineError): self.gate.evaluate(candidate() | {"quantity": True})
        with self.assertRaises(MachineError): self.gate.evaluate(candidate() | {"now_ns": 2**64})

    def test_changed_condition_changes_native_candidate_admission(self):
        for delta in [{"available_quote": 10019999}, {"expected_sequence": 8}, {"oracle_valid": 0},
                      {"observed_ns": 899}, {"price": 10016000}]:
            with self.subTest(delta=delta): self.assertFalse(self.gate.evaluate(candidate() | delta)["accepted"])

    def test_typed_program_compiles_and_runs_without_model_or_authority(self):
        compiled = self.vm.compile(program())
        result = self.vm.evaluate({"program": program(), "frame": frame()})
        self.assertTrue(compiled["accepted"])
        self.assertEqual(result["candidate"], "REDUCE")
        self.assertEqual(result["score_microunits"], 900000)
        self.assertEqual(result["language_model_calls"], 0)
        self.assertEqual(result["execution_authority"], "NONE")
        self.assertEqual(result["program_sha256"], compiled["program_sha256"])

    def test_unknown_expired_or_changed_state_abstains(self):
        for update, code in [({"valid": 0}, "STATE_INVALID"), ({"now_ns": 1100}, "EXPIRED"),
                             ({"now_ns": 1001}, "STALE"), ({"values": [800000]}, "ASSERTION_FAILED")]:
            with self.subTest(update=update):
                result = self.vm.evaluate({"program": program(), "frame": frame() | update})
                self.assertFalse(result["accepted"])
                self.assertEqual(result["code"], code)
                self.assertIsNone(result["candidate"])

    def test_incompatible_units_uninitialized_and_unbounded_programs_fail(self):
        bad = copy.deepcopy(program()); bad["instructions"][1]["unit"] = "money"
        self.assertEqual(self.vm.compile(bad)["code"], "UNIT_MISMATCH")
        bad = copy.deepcopy(program()); bad["instructions"][2]["a"] = 31
        self.assertEqual(self.vm.compile(bad)["code"], "UNINITIALIZED")
        with self.assertRaises(MachineError): self.vm.compile(program() | {"instructions": program()["instructions"] * 30})
        with self.assertRaises(MachineError): self.vm.evaluate({"program": program(), "frame": frame() | {"values": [True]}})
        with self.assertRaises(MachineError): self.vm.compile(program() | {"version": True})

    def test_http_scope_boundary_connects_real_program_abi(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "ENGINE_NATIVE_LIBRARY": str(self.path), "ENGINE_NATIVE_SHA256": self.checksum}):
            app = create_app(Path(folder) / "commerce.db")
            with TestClient(app) as owner, TestClient(app) as agent:
                owner.post("/api/sessions", json={})
                self.assertTrue(owner.get("/api/engine/overview").json()["native"]["enabled"])
                key = owner.post("/api/keys", json={"name": "native researcher", "scopes": ["engine:write"],
                    "policy_id": None, "ttl_seconds": 3600}).json()
                agent.headers["Authorization"] = "Bearer " + key["secret"]
                compiled = agent.post("/api/engine/native/programs/compile", json=program())
                self.assertEqual(compiled.status_code, 200, compiled.text)
                result = agent.post("/api/engine/native/programs/evaluate", json={"program": program(), "frame": frame()})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["candidate"], "REDUCE")
                self.assertEqual(agent.post("/api/engine/trade/orders/anything/approve", json={}).status_code, 403)
                key = owner.post("/api/keys", json={"name": "reader", "scopes": ["engine:read"],
                    "policy_id": None, "ttl_seconds": 3600}).json()
                agent.headers["Authorization"] = "Bearer " + key["secret"]
                self.assertEqual(agent.post("/api/engine/native/programs/evaluate", json={"program": program(), "frame": frame()}).status_code, 403)

    def test_public_playground_uses_real_native_library_and_never_creates_owner_state(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "ENGINE_NATIVE_LIBRARY": str(self.path), "ENGINE_NATIVE_SHA256": self.checksum}):
            with TestClient(create_portal(folder)) as client:
                source = client.get("/demo/native-program/example").json()["example"]
                first = client.post("/demo/native-program", json=source)
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(first.json()["candidate"], "REDUCE")
                self.assertEqual(first.json()["execution_authority"], "NONE")
                self.assertNotIn("set-cookie", first.headers)
                source["frame"]["values"] = [800000]
                self.assertEqual(client.post("/demo/native-program", json=source).json()["code"], "ASSERTION_FAILED")
                self.assertEqual(client.post("/demo/native-program", content="x" * 32001).status_code, 413)
                self.assertEqual(client.post("/demo/native-program", json={}).status_code, 400)
                self.assertEqual(list(Path(folder).iterdir()), [])
