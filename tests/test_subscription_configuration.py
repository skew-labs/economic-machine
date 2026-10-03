import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from configure_subscription import configuration
from economic_machine.values import MachineError


class SubscriptionConfigurationTests(unittest.TestCase):
    def test_approved_public_address_builds_exact_plan_and_keyless_facilitator(self):
        resources, merchants = configuration({}, {}, "0x" + "11" * 20, "https://machine.example", "https://rpc.example")
        p = resources["atlas-monthly"]
        self.assertEqual(p["network"], "eip155:42161")
        self.assertEqual(p["seller_owner"], "merchant-atlas-monthly")
        self.assertEqual(p["pay_to"], "0x" + "11" * 20)
        self.assertEqual(merchants["atlas-monthly"]["facilitator_url"], "https://facilitator.payai.network")
        self.assertEqual(configuration(resources, merchants, p["pay_to"], "https://machine.example", "https://rpc.example"), (resources, merchants))

    def test_missing_address_wrong_network_or_implicit_recipient_change_rejected(self):
        for recipient in [None, "", "0x" + "0" * 40, "private-key"]:
            with self.assertRaises(MachineError): configuration({}, {}, recipient, "https://machine.example", "https://rpc.example")
        resources, merchants = configuration({}, {}, "0x" + "11" * 20, "https://machine.example", "https://rpc.example")
        with self.assertRaises(MachineError): configuration(resources, merchants, "0x" + "22" * 20, "https://machine.example", "https://rpc.example")
        with self.assertRaises(MachineError): configuration({}, {}, "0x" + "11" * 20, "https://machine.example", "https://rpc.example", "eip155:8453")
