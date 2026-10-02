import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from compare_qwen import MeteredQwen, cheapest, compatible, expected_policy, supplier_book


class WorkloadTests(unittest.TestCase):
    def test_predefined_constraints_have_24_of_32_feasible_events(self):
        policy = expected_policy("TEST_CREDIT")
        book = supplier_book(1_790_900_000, "TEST_CREDIT")
        feasible = 0
        for event in range(32):
            raw = {**policy, "data_type": f"market.batch-{event % 8}"}
            candidates = [item for item in book if item["raw"]["data_type"] == raw["data_type"]]
            selected = cheapest(raw, candidates, 1_790_900_000)
            feasible += selected is not None
            if selected is not None:
                self.assertTrue(selected["id"].endswith("-4"))
                self.assertEqual(sum(compatible(raw, item, 1_790_900_000) for item in candidates), 2)
        self.assertEqual(feasible, 24)

    def test_robustness_book_has_unique_categories_and_same_constraints(self):
        policy = expected_policy("TEST_CREDIT")
        book = supplier_book(1_790_900_000, "TEST_CREDIT", 16)
        self.assertEqual(len(book), 128)
        feasible = sum(cheapest({**policy, "data_type": f"market.batch-{i}"}, book, 1_790_900_000)
                       is not None for i in range(16))
        self.assertEqual(feasible, 12)


class MeterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name)
        self.meter = MeteredQwen("private-test-key", self.run, 2, "0.25")
        await self.meter.client.aclose()

    async def asyncTearDown(self):
        await self.meter.client.aclose()
        self.temp.cleanup()

    async def mock(self, response):
        def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer private-test-key")
            return httpx.Response(200, json=response)
        self.meter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_real_protocol_usage_fields_are_saved_without_secrets(self):
        await self.mock({"id": "request-1", "model": "qwen3-32b", "usage": {
            "prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25, "cost": ".000003"},
            "choices": [{"finish_reason": "stop", "message": {"content": '{"seller_id":null}'}}]})
        value, call = await self.meter.call("test", [{"role": "user", "content": "choose"}])
        self.assertEqual(value, {"seller_id": None})
        self.assertEqual(call["usage"]["total_tokens"], 25)
        log = (self.run / "provider-calls.jsonl").read_text()
        self.assertNotIn("private-test-key", log)
        self.assertNotIn("Authorization", log)
        self.assertEqual(json.loads(log)["provider_id"], "request-1")

    async def test_output_truncation_still_counts_charged_tokens(self):
        await self.mock({"model": "qwen3-32b", "usage": {"prompt_tokens": 20,
            "completion_tokens": 4096, "total_tokens": 4116}, "choices": [
            {"finish_reason": "length", "message": {"content": ""}}]})
        value, call = await self.meter.call("test", [])
        self.assertIsNone(value)
        self.assertEqual(call["status"], "INVALID_FINAL_OUTPUT")
        self.assertEqual(call["usage"]["total_tokens"], 4116)
        self.assertIsNone(call["usage"]["cost_usd"])

    async def test_request_cap_blocks_before_transmitting(self):
        await self.mock({"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})
        await self.meter.call("one", [])
        await self.meter.call("two", [])
        with self.assertRaises(RuntimeError):
            await self.meter.call("three", [])
        self.assertEqual(len(self.meter.calls), 2)

    async def test_budget_cap_blocks_before_transmitting(self):
        self.meter.max_cost = 0
        with self.assertRaises(RuntimeError):
            await self.meter.call("over-budget", [])
        self.assertEqual(self.meter.calls, [])

    async def test_transport_failure_keeps_unknown_billing(self):
        def fail(request):
            raise httpx.ReadTimeout("private provider response unavailable")
        self.meter.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        value, call = await self.meter.call("lost", [])
        self.assertIsNone(value)
        self.assertIsNone(call["usage"]["total_tokens"])
        self.assertEqual(call["status"], "TRANSPORT_UNKNOWN_BILLING")
        self.assertNotIn("private provider", (self.run / "provider-calls.jsonl").read_text())

    async def test_thinking_baseline_gets_larger_budget_and_official_sampling(self):
        self.meter.thinking = True
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [
                {"finish_reason": "stop", "message": {"content": '{"seller_id":null}'}}]})
        self.meter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.meter.call("direct:0:0", [])
        await self.meter.call("shared_policy_compiler", [])
        self.assertEqual((bodies[0]["max_tokens"], bodies[0]["temperature"], bodies[0]["top_p"]), (8192, .6, .95))
        self.assertEqual((bodies[1]["max_tokens"], bodies[1]["temperature"], bodies[1]["top_p"]), (4096, .7, .8))
