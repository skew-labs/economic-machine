import base64
import copy
import json
import unittest

from economic_machine.values import MachineError
from machine_commerce.x402 import PaymentBinding, admit_required, decode_header, inspect_response


def header(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


class X402Tests(unittest.TestCase):
    def setUp(self):
        self.binding = PaymentBinding("ab" * 32, "https://seller.example/data/version-1", "eip155:421614",
                                      "0x" + "11" * 20, "0x" + "22" * 20, 400000, 1000)
        self.required = {"x402Version": 2, "resource": {"url": self.binding.resource_url},
                         "accepts": [{"scheme": "exact", "network": self.binding.network,
                                      "amount": "400000", "asset": self.binding.asset,
                                      "payTo": self.binding.pay_to, "maxTimeoutSeconds": 60,
                                      "extra": {"name": "USDC", "version": "2"}}]}

    def admit(self, required=None, now=900, terms=None):
        return admit_required(header(self.required if required is None else required), self.binding,
                              self.binding.terms_hash if terms is None else terms, now)

    def test_exact_agreed_challenge_is_ready_for_signature_but_unpaid(self):
        result = self.admit()
        self.assertEqual(result["status"], "SIGNATURE_REQUIRED")
        self.assertEqual(result["payment_status"], "UNPAID")
        self.assertIsNone(result["tx_hash"])
        self.assertEqual(result["authorization_expires"], 960)

    def test_untrusted_resource_recipient_asset_price_and_network_cannot_change_terms(self):
        for key, value in [("payTo", "0x" + "ff" * 20), ("asset", "0x" + "ff" * 20),
                           ("amount", "400001"), ("network", "eip155:1"), ("maxTimeoutSeconds", 3600),
                           ("scheme", "upto")]:
            with self.subTest(key=key), self.assertRaises(MachineError):
                changed = copy.deepcopy(self.required)
                changed["accepts"][0][key] = value
                self.admit(changed)
        changed = copy.deepcopy(self.required)
        changed["resource"]["url"] = "https://other.example/data"
        with self.assertRaises(MachineError):
            self.admit(changed)

    def test_expiry_version_and_ambiguous_options_are_rejected(self):
        for now, terms in [(1000, self.binding.terms_hash), (900, "cd" * 32)]:
            with self.assertRaises(MachineError):
                self.admit(now=now, terms=terms)
        changed = copy.deepcopy(self.required)
        changed["accepts"] *= 2
        with self.assertRaises(MachineError):
            self.admit(changed)
        changed["x402Version"] = True
        with self.assertRaises(MachineError):
            self.admit(changed)

    def test_unsupported_prepaid_flow_or_permit2_cannot_be_silently_admitted(self):
        for extra in [{"paymentFlow": "settlement"}, {"assetTransferMethod": "permit2"},
                      {"name": "DifferentToken", "version": "2"}]:
            changed = copy.deepcopy(self.required)
            changed["accepts"][0]["extra"] = extra
            with self.assertRaises(MachineError):
                self.admit(changed)

    def test_invalid_base64_duplicate_keys_and_test_credits_are_rejected(self):
        duplicate = base64.b64encode(b'{"x402Version":2,"x402Version":1}').decode()
        for value in ["!", "x" * 16385, duplicate, header([])]:
            with self.assertRaises(MachineError):
                decode_header(value)
        bad = PaymentBinding(self.binding.terms_hash, self.binding.resource_url, self.binding.network,
                             "TEST_CREDIT", self.binding.pay_to, 400000, 1000)
        with self.assertRaises(MachineError):
            bad.validate()

    def test_success_header_requires_chain_reconciliation_and_cannot_trigger_retry(self):
        response = {"success": True, "transaction": "0x" + "33" * 32, "network": self.binding.network}
        result = inspect_response(header(response), self.binding)
        self.assertEqual(result["payment_status"], "SETTLEMENT_REPORTED")
        self.assertEqual(result["chain_reconciliation"], "REQUIRED")
        self.assertFalse(result["safe_to_retry_payment"])
        response.update(success=False, errorReason="settlement_pending")
        self.assertFalse(inspect_response(header(response), self.binding)["safe_to_retry_payment"])

    def test_missing_or_wrong_settlement_evidence_cannot_be_reported_as_paid(self):
        for response in [{"success": True, "transaction": "", "network": self.binding.network},
                         {"success": False, "transaction": "", "network": self.binding.network,
                          "errorReason": "settlement_pending"},
                         {"success": True, "transaction": "0x" + "33" * 32, "network": "eip155:1"}]:
            with self.assertRaises(MachineError):
                inspect_response(header(response), self.binding)
