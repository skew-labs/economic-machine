import base64
import json
import tempfile
import unittest
from pathlib import Path

from economic_machine.values import MachineError
from machine_commerce.compute_registry import ComputeRegistry, NETWORK, URL, USDC
from machine_commerce.store import Store


class Transport:
    def __init__(self):
        self.calls = []
        self.status = 402
        self.body = {"x402Version": 2, "resource": {"url": URL}, "accepts": [{"scheme": "exact",
            "network": NETWORK, "asset": USDC, "payTo": "0x" + "22" * 20, "amount": "1000",
            "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}]}

    def call(self, url, body, headers=None):
        self.calls.append((url, body, headers))
        return self.status, {"payment-required": base64.b64encode(json.dumps(self.body).encode()).decode()}, b"{}"


class ComputeRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1791040000
        self.store = Store(Path(self.tmp.name) / "compute.db", lambda: self.now)
        self.transport = Transport()
        self.registry = ComputeRegistry(self.store, self.transport)

    def test_keyless_unsigned_price_is_observed_without_claiming_capacity_or_delivery(self):
        result = self.registry.probe("gate402-inference")
        self.assertEqual(result["status"], "QUOTE_OBSERVED")
        self.assertEqual(result["quote"]["amount_atoms"], "1000")
        self.assertFalse(result["purchase_enabled"])
        self.assertFalse(result["capacity_verified"])
        self.assertEqual(result["signatures_sent"], 0)
        self.assertEqual(self.transport.calls[0][2], {"x-payment-network": NETWORK})
        self.assertEqual(self.registry.probe("gate402-inference"), result)
        self.assertEqual(len(self.transport.calls), 1)
        self.now += 120
        self.registry.probe("gate402-inference")
        self.assertEqual(len(self.transport.calls), 2)

    def test_wrong_network_amount_domain_or_duplicate_options_remain_unavailable(self):
        for field, value in [("network", "eip155:8453"), ("asset", "0x" + "11" * 20),
                             ("amount", "99999999999"), ("amount", "01000"), ("payTo", "0x" + "0" * 40),
                             ("maxTimeoutSeconds", True), ("extra", {"name": "FAKE", "version": "2"})]:
            with self.subTest(field=field):
                transport = Transport()
                transport.body["accepts"][0][field] = value
                self.assertEqual(ComputeRegistry(self.store, transport).probe("gate402-inference")["status"], "UNAVAILABLE")
        transport = Transport()
        transport.body["accepts"] *= 2
        self.assertEqual(ComputeRegistry(self.store, transport).probe("gate402-inference")["status"], "UNAVAILABLE")

    def test_response_success_or_wrong_resource_is_not_paid_compute_evidence(self):
        self.transport.status = 200
        self.assertEqual(self.registry.probe("gate402-inference")["status"], "UNAVAILABLE")
        self.transport = Transport()
        self.transport.body["resource"]["url"] = "https://evil.example"
        self.assertEqual(ComputeRegistry(self.store, self.transport).probe("gate402-inference")["status"], "UNAVAILABLE")
        with self.assertRaises(MachineError): self.registry.probe("custom-url")

    def test_discovery_examples_with_float_prices_do_not_enter_payment_math(self):
        self.transport.body["extensions"] = {"example": {"prepaidUsdc": 0.001}}
        result = self.registry.probe("gate402-inference")
        self.assertEqual(result["status"], "QUOTE_OBSERVED")
        self.assertEqual(result["quote"]["amount_atoms"], "1000")
        self.assertEqual(result["challenge_hash_scheme"], "SHA256_RAW_X402_JSON_BYTES")
        self.assertEqual(len(result["challenge_hash"]), 64)
