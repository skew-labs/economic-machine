"""Adversarial joins and public replay boundary, using the actual recorded trade."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from economic_machine.values import digest
from machine_commerce.evidence import build_completed_trade, replay_policy, validate_bundle
from machine_commerce.portal import create_portal

FIXTURE = Path(__file__).parent / "fixtures/completed-trade.json"


def fixture():
    return json.loads(FIXTURE.read_text())


def build(value=None):
    value = value or fixture()
    return build_completed_trade(**value, checked_at=1790940000)


class TradeEvidence(unittest.TestCase):
    def rejects(self, change, message):
        value = fixture()
        change(value)
        with self.assertRaisesRegex(ValueError, message):
            build(value)

    def test_actual_runtime_chain_and_delivery_are_joined(self):
        bundle = validate_bundle(build())
        self.assertEqual(bundle["payment"]["status"], "SETTLED")
        self.assertEqual(bundle["capital"]["spent_atoms"], 10000)
        self.assertEqual(bundle["capital"]["reserved_atoms"], 0)
        self.assertEqual(bundle["agreement"]["terms_hash"], bundle["delivery"]["artifact"]["terms_hash"])
        self.assertTrue(bundle["authorization"]["recovered_from_chain"])
        self.assertFalse(bundle["authorization"]["customer_browser_signature"])
        self.assertIsNone(bundle["deployment"]["custom_contract_address"])
        self.assertFalse(bundle["deployment"]["competition_eligibility_certified"])

    def test_wrong_trade_cannot_borrow_another_transaction(self):
        self.rejects(lambda f: f["record"]["payment"].update(tx_hash="0x" + "ab" * 32), "completed owned")
        self.rejects(lambda f: f["record"]["match"].update(supply_id="other"), "record IDs")
        self.rejects(lambda f: f["record"]["payment"].update(owner="other"), "completed owned")

    def test_changed_terms_or_merchant_request_are_rejected(self):
        self.rejects(lambda f: f["record"]["match"]["body"]["terms"].update(total_price="0.02"), "terms hash")
        self.rejects(lambda f: f["record"]["payment"]["body"]["request"].update(license="commercial-use"), "merchant request")

    def test_payer_recipient_and_nonce_are_bound(self):
        for field in ["from", "to", "nonce"]:
            with self.subTest(field=field):
                self.rejects(lambda f, field=field: f["record"]["payment"]["body"]["authorization"].update({field: "different"}),
                             "authorization does not match")

    def test_capital_and_expiry_are_real_record_values(self):
        self.rejects(lambda f: f["record"]["mandate"].update(reserved=1), "settled capital")
        self.rejects(lambda f: f["record"]["mandate"].update(spent=0), "settled capital")
        self.rejects(lambda f: f["record"]["mandate"].update(expires=1), "already expired")

    def test_rehashed_terms_still_must_match_policy(self):
        value = fixture()
        terms = value["record"]["match"]["body"]["terms"]
        terms["license"] = "commercial-use"
        new_hash = digest(terms)
        value["record"]["match"]["terms_hash"] = new_hash
        value["record"]["proof"]["terms_hash"] = new_hash
        value["record"]["payment"]["body"]["binding"]["terms_hash"] = new_hash
        value["record"]["payment"]["body"]["request"].update(terms_hash=new_hash, license="commercial-use")
        with self.assertRaisesRegex(ValueError, "policy replay differs"):
            build(value)

    def test_delivery_must_match_the_original_received_payload(self):
        self.rejects(lambda f: f["record"]["payment"].update(delivery=None), "delivery is not bound")
        self.rejects(lambda f: f["record"]["payment"]["delivery"]["artifact"]["data"].update(gas_used=0), "delivery is not bound")

    def test_unfinalized_or_wrong_runtime_receipt_is_rejected(self):
        for field, changed in [("finality", "latest"), ("amount_atoms", "20000"), ("block_hash", "other")]:
            with self.subTest(field=field):
                self.rejects(lambda f, field=field, changed=changed: f["record"]["payment"]["observation"].update({field: changed}), "reconciliation disagrees")

    def test_two_distinct_rpc_receipts_are_required(self):
        self.rejects(lambda f: f["observations"].__setitem__(1, copy.deepcopy(f["observations"][0])), "distinct approved RPC")
        self.rejects(lambda f: f["observations"][0].update(finalized_block=1), "distinct approved RPC")
        self.rejects(lambda f: f["observations"][0].update(nonce_consumed_at_finalized=False), "distinct approved RPC")

    def test_signature_must_match_both_payer_and_runtime_payload(self):
        self.rejects(lambda f: f["signature_checks"][0].update(payload_hash="different"), "on-chain signature")
        self.rejects(lambda f: f["signature_checks"][1].update(recovered_payer="other"), "on-chain signature")

    def test_historical_balances_are_labelled_preserved_not_fresh(self):
        bundle = build()
        self.assertIn("NOT_A_FRESH", bundle["capital"]["balance_evidence"])
        self.assertTrue(all(o["historical_balances_match"] is None for o in bundle["audit"]["rpc_observations"]))
        self.assertEqual(bundle["audit"]["preserved_audit_hash"], digest(fixture()["archived_audit"]))
        self.rejects(lambda f: f["archived_audit"].update(tx_hash="other"), "historical balance audit")
        self.rejects(lambda f: f["archived_audit"]["observations"][0].update(historical_balances_match=False), "historical balance audit")

    def test_journal_and_transition_order_are_required(self):
        self.rejects(lambda f: f["record"].update(journal_verified=False), "journal integrity")
        self.rejects(lambda f: f["record"]["events"].pop(0), "missing or duplicated")
        self.rejects(lambda f: f["record"]["events"].append(copy.deepcopy(f["record"]["events"][0])), "event order")
        self.rejects(lambda f: f["record"]["events"][0]["event"].update(payment_id="other"), "event payment ID")
        self.rejects(lambda f: f["record"]["events"][2]["event"].update(payload_hash="other"), "submission or delivery")

    def test_public_projection_cannot_leak_added_credential_fields(self):
        value = fixture()
        value["record"]["payment"]["private_key"] = "DO_NOT_PUBLISH_KEY"
        value["record"]["mandate"]["password"] = "DO_NOT_PUBLISH_PASSWORD"
        serialized = json.dumps(build(value))
        self.assertNotIn("DO_NOT_PUBLISH", serialized)
        self.assertNotIn('"private_key"', serialized)
        self.assertNotIn('"signature":', serialized)

    def test_modified_public_bundle_fails_closed(self):
        bundle = build()
        bundle["payment"]["amount"] = "999"
        with self.assertRaisesRegex(ValueError, "bundle changed"):
            validate_bundle(bundle)


class ReplayAndHTTP(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.site = Path(self.directory.name)
        self.evidence = self.site / "trade.json"
        self.bundle = build()
        self.evidence.write_text(json.dumps(self.bundle))
        self.client = TestClient(create_portal(self.site, evidence_path=self.evidence))
        self.policy = {"budget": "0.01", "max_age_seconds": 86400, "license": "internal-use"}

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def test_original_policy_replays_without_execution_authority(self):
        before = self.evidence.read_bytes()
        for _ in range(2):
            result = self.client.post("/demo/trade/replay", json=self.policy).json()
            self.assertEqual(result["result"]["status"], "AGREED")
            self.assertEqual(result["result"]["terms"]["total_price"], "0.01")
            self.assertEqual(result["execution_authority"], "NONE")
            self.assertFalse(result["new_payment"])
        self.assertEqual(before, self.evidence.read_bytes())

    def test_budget_age_and_license_each_reject_the_same_offer(self):
        for field, value, code in [("budget", "0.001", "PRICE_OUTSIDE_BUYER_POLICY"),
                                   ("max_age_seconds", 5, "DATA_NOT_FRESH"),
                                   ("license", "commercial-use", "LICENSE_NOT_GRANTED")]:
            with self.subTest(field=field):
                policy = self.policy | {field: value}
                result = replay_policy(self.bundle, policy)
                self.assertEqual(result["result"]["status"], "ESCALATE")
                self.assertIn(code, result["result"]["reason_codes"])

    def test_closed_inputs_do_not_become_payments(self):
        for raw in [[], {}, self.policy | {"execute": True}, self.policy | {"license": []},
                    self.policy | {"budget": "0"}, self.policy | {"budget": "2"},
                    self.policy | {"budget": 0.01}, self.policy | {"max_age_seconds": True}]:
            with self.subTest(raw=raw):
                self.assertEqual(self.client.post("/demo/trade/replay", json=raw).status_code, 400)
        self.assertEqual(self.client.post("/demo/trade/replay", content="not-json").status_code, 400)
        self.assertEqual(self.client.post("/demo/trade/replay", content="x" * 1025).status_code, 413)

    def test_evidence_and_delivery_are_exact_read_only_downloads(self):
        response = self.client.get("/demo/trade", headers={"Origin": "https://example.org"})
        self.assertEqual(response.json(), self.bundle)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertNotIn("set-cookie", response.headers)
        response = self.client.get("/demo/trade/artifact")
        self.assertEqual(digest(response.json()), self.bundle["delivery"]["artifact_hash"])
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(self.client.post("/demo/trade", json={}).status_code, 405)

    def test_missing_corrupt_or_tampered_evidence_is_unavailable(self):
        for text in ["not-json", '{}', '[]', 'null', json.dumps(self.bundle | {"bundle_hash": "other"})]:
            self.evidence.write_text(text)
            for path in ["/demo/trade", "/demo/trade/artifact"]:
                self.assertEqual(self.client.get(path).status_code, 503)
            self.assertEqual(self.client.post("/demo/trade/replay", json=self.policy).status_code, 503)
        self.evidence.unlink()
        self.assertEqual(self.client.get("/demo/trade").status_code, 503)

    def test_submission_assets_and_csp_are_bound_to_portal(self):
        response = self.client.get("/submission")
        self.assertIn('src="/commerce/submission.js', response.text)
        self.assertIn("Recorded purchase", response.text)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        for path in ["/submission.js", "/submission.css"]:
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/fixtures/completed-trade.json").status_code, 404)


if __name__ == "__main__":
    unittest.main()
