import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from contract import (MODEL, NOTICE_VERSION, DATA_SCOPE, ActivationState, PUBLIC_BOUNDS,
                      Rejected, build_request, validate_response)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.frame = json.loads((Path(__file__).parent / "local-frame.example.json").read_text())
        self.public = json.loads((Path(__file__).parent / "request.example.json").read_text())["public_frame"]
        self.policy = dict(frame_id=self.frame["frame_id"], mode="QUOTE", target_inventory_lots=0,
                           half_spread_ticks=3, alpha_ticks=0, inventory_skew_ticks=0, clip_lots=1,
                           levels_per_side=2, reprice_threshold_ticks=1, ttl_ms=5000, reason_code="BALANCED")
        self.now = self.frame["observed_monotonic_ns"] + 10_000_000
        self.deadline = self.now + 2_000_000_000
        self.activation = ActivationState(enabled=True, training_acknowledged=True,
                                          notice_version=NOTICE_VERSION, data_scope=DATA_SCOPE,
                                          eligible=True, source_use_approved=True,
                                          expires_ns=self.deadline + 1_000_000_000)

    def request(self, frame=None, activation=None):
        return build_request(self.public if frame is None else frame,
                             activation=self.activation if activation is None else activation, now_ns=self.now)

    def response(self, policy=None):
        return {"model": MODEL, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(policy or self.policy)}}]}

    def validate(self, response=None, frame=None, now=None):
        return validate_response(response or self.response(), frame or self.frame,
                                 now_ns=self.now if now is None else now, deadline_ns=self.deadline,
                                 activation=self.activation)

    def test_request_is_complete_and_model_pinned(self):
        request = self.request()
        self.assertEqual(request["model"], MODEL)
        self.assertEqual(request["reasoning_effort"], "minimal")
        self.assertNotIn("content_file", request["messages"][0])
        self.assertIn("schema", request["response_format"]["json_schema"])
        payload = json.loads(request["messages"][1]["content"])
        self.assertEqual(payload["features"], self.public["features"])
        self.assertEqual(payload["bounds"], PUBLIC_BOUNDS)
        self.assertEqual(set(payload), {"frame_id", "market", "source_slot", "features", "bounds"})
        self.assertRegex(payload["frame_id"], r"^[a-f0-9]{32}$")
        self.assertNotEqual(payload["frame_id"], self.frame["frame_id"])

    def test_valid_output_is_never_authority(self):
        result = self.validate()
        self.assertFalse(result["execution_authority"])
        self.assertEqual(result["valid_until_monotonic_ns"], self.deadline)

    def test_model_fallback_and_incomplete_rejected(self):
        for changes in ({"model": "muse-spark-1.3"}, {"model": "other"}):
            with self.assertRaises(Rejected): self.validate(self.response() | changes)
        for reason in ("length", "tool_calls", None):
            response = self.response()
            response["choices"][0]["finish_reason"] = reason
            with self.assertRaises(Rejected): self.validate(response)

    def test_extra_keys_duplicate_keys_and_boolean_rejected(self):
        for policy in (self.policy | {"sign": True}, self.policy | {"clip_lots": True}, self.policy | {"clip_lots": 1 << 80}):
            with self.assertRaises(Rejected): self.validate(self.response(policy))
        response = self.response()
        response["choices"][0]["message"]["content"] = json.dumps(self.policy)[:-1] + ', "clip_lots": 1}'
        with self.assertRaises(Rejected): self.validate(response)

    def test_input_age_and_response_deadline(self):
        with self.assertRaises(Rejected): self.validate(now=self.deadline)
        with self.assertRaises(Rejected): self.validate(now=0)
        with self.assertRaises(Rejected): self.validate(self.response(self.policy | {"ttl_ms": 1}))
        with self.assertRaises(Rejected): self.validate(now=self.now + 1_000_000_000)

    def test_wrong_frame_and_unhealthy_state(self):
        with self.assertRaises(Rejected): self.validate(self.response(self.policy | {"frame_id": "different"}))
        for field in ("valid", "reconciled"):
            with self.assertRaises(Rejected): self.validate(frame=self.frame | {field: False})

    def test_inventory_bound_includes_both_sides(self):
        frame = copy.deepcopy(self.frame)
        frame["bounds"]["max_abs_position_lots"] = 4
        with self.assertRaises(Rejected): self.validate(frame=frame)

    def test_halt_and_hold_never_carry_quotes(self):
        for mode in ("HALT", "HOLD"):
            with self.assertRaises(Rejected): self.validate(self.response(self.policy | {"mode": mode}))
            policy = self.policy | {"mode": mode}
            for field in ("target_inventory_lots", "half_spread_ticks", "alpha_ticks", "inventory_skew_ticks", "clip_lots", "levels_per_side", "reprice_threshold_ticks"):
                policy[field] = 0
            self.assertEqual(self.validate(self.response(policy), frame=self.frame | {"valid": False})["candidate"]["mode"], mode)

    def test_model_cannot_request_personal_inventory_reduction(self):
        policy = self.policy | dict(mode="REDUCE", target_inventory_lots=0, half_spread_ticks=0,
                                   alpha_ticks=0, inventory_skew_ticks=0, levels_per_side=0, clip_lots=2)
        with self.assertRaises(Rejected): self.validate(self.response(policy))
        for patch in ({"target_inventory_lots": 1}, {"inventory_skew_ticks": 1}):
            with self.assertRaises(Rejected): self.validate(self.response(self.policy | patch))

    def test_refusal_tool_call_and_bad_json(self):
        for message in ({"refusal": "no", "content": "{}"}, {"tool_calls": [1], "content": "{}"}, {"content": "```json {} ```"}):
            response = self.response()
            response["choices"][0]["message"] = message
            with self.assertRaises(Rejected): self.validate(response)

    def test_halt_works_with_one_sided_inventory_mandate(self):
        frame = copy.deepcopy(self.frame)
        frame["bounds"]["target_inventory_min_lots"] = 1
        policy = self.policy | dict(mode="HALT", target_inventory_lots=0, half_spread_ticks=0,
                                   inventory_skew_ticks=0, clip_lots=0, levels_per_side=0,
                                   reprice_threshold_ticks=0)
        self.assertEqual(self.validate(self.response(policy), frame=frame)["candidate"]["mode"], "HALT")

    def test_default_disabled(self):
        with self.assertRaises(Rejected): build_request(self.public, now_ns=self.now)
        with self.assertRaises(Rejected): self.request(activation=ActivationState())
        with self.assertRaises(Rejected): self.request(activation={"enabled": True})

    def test_activation_scope_version_eligibility_and_expiry(self):
        for patch in (
            {"training_acknowledged": False}, {"notice_version": "old"},
            {"data_scope": "all-data"}, {"eligible": False}, {"source_use_approved": False},
            {"expires_ns": self.now}, {"revoked": True}, {"enabled": 1},
        ):
            with self.subTest(patch=patch), self.assertRaises(Rejected):
                self.request(activation=replace(self.activation, **patch))

    def test_revocation_blocks_late_response(self):
        self.request()
        self.activation = replace(self.activation, revoked=True)
        with self.assertRaises(Rejected): self.validate()

    def test_full_local_frame_is_not_a_provider_input(self):
        local = copy.deepcopy(self.frame)
        local["api_key"] = "SYNTHETIC_DO_NOT_SEND"
        with self.assertRaises(Rejected): self.request(frame=local)

    def test_extra_identity_credentials_or_private_data_rejected(self):
        for key in ("wallet", "email", "private_key", "api_key", "customer_id", "source_fork", "bounds"):
            with self.subTest(key=key), self.assertRaises(Rejected):
                self.request(frame=self.public | {key: "SYNTHETIC_DO_NOT_SEND"})
        for key in ("position_lots", "free_margin_atoms", "markout_bps_1s", "hedge_cost_bps", "authorization"):
            frame = copy.deepcopy(self.public)
            frame["features"][key] = 1
            with self.subTest(key=key), self.assertRaises(Rejected): self.request(frame=frame)

    def test_string_channels_and_numeric_type_confusion_rejected(self):
        with self.assertRaises(Rejected): self.request(frame=self.public | {"market": "SYNTHETIC_CUSTOMER_ID"})
        with self.assertRaises(Rejected): self.request(frame=self.public | {"source_slot": True})
        for value in ("SYNTHETIC_DO_NOT_SEND", True, 1 << 80):
            frame = copy.deepcopy(self.public)
            frame["features"]["spread_ticks"] = value
            with self.assertRaises(Rejected): self.request(frame=frame)

    def test_public_freshness_and_validity(self):
        for patch in ({"observed_monotonic_ns": self.now + 1}, {"observed_monotonic_ns": 0}, {"valid": False}):
            with self.assertRaises(Rejected): self.request(frame=self.public | patch)

    def test_request_nonce_is_not_stable_identity(self):
        first = json.loads(self.request()["messages"][1]["content"])["frame_id"]
        second = json.loads(self.request()["messages"][1]["content"])["frame_id"]
        self.assertNotEqual(first, second)

    def test_public_template_bounds_remain_enforced_with_loose_local_mandate(self):
        frame = copy.deepcopy(self.frame)
        frame["bounds"]["max_clip_lots"] = 100
        frame["bounds"]["max_abs_position_lots"] = 1000
        with self.assertRaises(Rejected): self.validate(self.response(self.policy | {"clip_lots": 3}), frame=frame)


if __name__ == "__main__":
    unittest.main()
