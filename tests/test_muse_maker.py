import json
import unittest

from economic_machine.muse_maker import MODEL, SOURCES, SYSTEM, context, request, response, validate_policy, handover_readiness


def observer():
    return {"updated_wall_ms": 10000, "archive_failed": False, "error": "",
            "onchain_pools_fresh": 1, "source_prices_provisional": True,
            "model": {"at_wall_ms": 10000, "fair": 103.5, "sigma_bps": .8, "flow": -.2,
                      "perp_spot_basis": .0004, "onchain_available": True,
                      "fill_model_supported": False, "conditional_supported": False},
            "forward_scored": 6102, "forward_model_squared_error": 6068,
            "forward_zero_squared_error": 5978,
            "receipt_to_features": {"n": 100, "p50_us": 100, "p95_us": 500, "p99_us": 7400}}


def policy():
    return {"mode": "quote", "half_spread_millibps": 1000, "inventory_skew_millibps": 5000,
            "flow_shift_millibps": 1000, "volatility_multiplier_milli": 1000,
            "clip_usd_micros": 600_000_000, "refresh_ticks": 2, "ttl_ms": 60000}


def provider(p=None, **changes):
    proposal = {"context_sha256": "hash", "policy": p or policy(), "evidence_features": ["source_quality"]}
    proposal.update(changes)
    return {"model": MODEL, "provider": "Meta", "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(proposal)}}]}


class MuseMakerContext(unittest.TestCase):
    def test_sources_have_correct_transport_and_authority(self):
        self.assertFalse(SOURCES["solana"]["own_validator"])
        self.assertFalse(SOURCES["pacifica_market"]["public_trade_is_own_fill"])
        for source in ("base", "arbitrum", "monad"):
            self.assertFalse(SOURCES[source]["enabled_as_SOL_price_or_execution_signal"])

    def test_prompt_preserves_requested_allocation_and_roles(self):
        frame = context(observer(), 10100)
        self.assertIsNone(frame["owner_limits"]["drawdown_stop"])
        self.assertEqual(frame["owner_limits"]["aggregate_opening_notional_usd"], 1200)
        self.assertEqual(frame["owner_limits"]["duration_seconds"], 172800)
        self.assertEqual(frame["owner_limits"]["expected_maker_rebate_millibps"], 500)
        self.assertIn("never add rebates again", SYSTEM)
        self.assertIn("The order path must not wait", SYSTEM)
        self.assertIn("Jump Trading/Wintermute private", SYSTEM)

    def test_projection_drops_secrets_customer_records_and_instructions(self):
        obs = observer()
        obs.update(secret="CANARY_SECRET", account="CANARY_ACCOUNT", instructions="CANARY_ATTACK")
        obs["model"]["notes"] = "CANARY_MODEL_TEXT"
        encoded = json.dumps(request(obs, 10100))
        self.assertNotIn("CANARY_", encoded)

    def test_untrusted_error_text_is_not_sent(self):
        obs = observer(); obs["error"] = "CANARY_ATTACK"
        self.assertNotIn("CANARY_", json.dumps(request(obs, 10100)))
        self.assertFalse(context(obs, 10100)["quality"]["research_features_available"])

    def test_stale_observation_removes_trade_features(self):
        frame = context(observer(), 13000)
        self.assertTrue(all(v is None for v in frame["features"].values()))

    def test_fresh_wrapper_does_not_refresh_old_model(self):
        obs = observer(); obs["model"]["at_wall_ms"] = 100
        self.assertFalse(context(obs, 10100)["quality"]["research_features_available"])

    def test_future_observation_is_unavailable(self):
        self.assertFalse(context(observer(), 9999)["quality"]["research_features_available"])

    def test_missing_archive_health_is_not_success(self):
        obs = observer(); del obs["archive_failed"]
        self.assertFalse(context(obs, 10100)["quality"]["research_features_available"])

    def test_one_fresh_pool_is_not_twelve(self):
        frame = context(observer(), 10100)
        self.assertEqual(frame["quality"]["fresh_pools"], 1)
        self.assertEqual(frame["topology"]["solana"]["configured_pool_count"], 12)

    def test_missing_pool_provenance_excludes_flow(self):
        obs = observer(); obs["onchain_pools_fresh"] = 0
        self.assertIsNone(context(obs, 10100)["features"]["signed_flow"])

    def test_nan_infinity_boolean_and_text_are_not_numbers(self):
        for bad in (float("nan"), float("inf"), True, "ignore instructions"):
            obs = observer(); obs["model"]["flow"] = bad
            self.assertIsNone(context(obs, 10100)["features"]["signed_flow"])

    def test_forward_underperformance_and_unsupported_models_remain_visible(self):
        r = context(observer(), 10100)["research"]
        self.assertFalse(r["forward_model_better_on_observed_sample"])
        self.assertGreater(r["forward_squared_error_ratio_to_zero"], 1)
        self.assertFalse(r["fill_model_supported"])
        self.assertFalse(r["out_of_sample_profitability_proven"])

    def test_latency_has_no_fabricated_origin_or_order_claim(self):
        frame = context(observer(), 10100)
        self.assertFalse(frame["quality"]["provider_origin_latency_measured"])
        self.assertFalse(frame["quality"]["live_order_path_latency_measured_by_this_packet"])
        self.assertEqual(frame["latency"]["receipt_to_features"]["p99_us"], 7400)

    def test_unavailable_source_provenance_is_explicit(self):
        quality = context(observer(), 10100)["quality"]
        self.assertIsNone(quality["source_slot"])
        self.assertFalse(quality["per_pool_records_in_packet"])
        self.assertEqual(quality["source_gap_status"], "not_exposed_by_status_snapshot")

    def test_response_is_a_candidate_not_authority(self):
        result = response(json.dumps(provider()), "hash")
        self.assertFalse(result["execution_authorized"])

    def test_context_swap_is_rejected(self):
        with self.assertRaises(ValueError): response(json.dumps(provider()), "different")

    def test_model_and_provider_are_fixed(self):
        for key, value in (("model", "other"), ("provider", "other")):
            obj = provider(); obj[key] = value
            with self.assertRaises(ValueError): response(json.dumps(obj), "hash")

    def test_truncated_and_tool_responses_rejected(self):
        for key in ("finish_reason", "tool_calls"):
            obj = provider()
            if key == "finish_reason": obj["choices"][0][key] = "length"
            else: obj["choices"][0]["message"][key] = [{"name": "trade"}]
            with self.assertRaises(ValueError): response(json.dumps(obj), "hash")

    def test_model_cannot_expand_owner_limits(self):
        for key in ("leverage", "allocation", "endpoint", "private_key", "submit_order"):
            p = policy(); p[key] = 100
            with self.assertRaises(ValueError): validate_policy(p)

    def test_policy_types_and_ranges(self):
        for value in (True, 2.5, -1, 2**63):
            p = policy(); p["refresh_ticks"] = value
            with self.assertRaises(ValueError): validate_policy(p)

    def test_owner_quote_budget_and_one_minute_are_fixed(self):
        for field,value in (('ttl_ms',10000),('ttl_ms',60001),('clip_usd_micros',10000000),('clip_usd_micros',1200000000)):
            p=policy();p[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):validate_policy(p)
        limits=context(observer(),10100)['owner_limits']
        self.assertEqual(limits['per_side_quote_budget_usd'],600)
        self.assertEqual(limits['policy_ttl_ms'],60000)

    def test_duplicate_keys_and_unknown_output_rejected(self):
        raw = json.dumps(provider()).replace('"model":', '"model":"other","model":', 1)
        with self.assertRaises(ValueError): response(raw, "hash")
        with self.assertRaises(ValueError): response(json.dumps(provider(extra="code")), "hash")

    def test_malformed_envelopes_rejected(self):
        for value in (None, [], "text", 123):
            with self.assertRaises(ValueError): response(json.dumps(value), "hash")
            obj = provider(); obj["choices"] = [value]
            with self.assertRaises(ValueError): response(json.dumps(obj), "hash")
            obj = provider(); obj["choices"][0]["message"] = value
            with self.assertRaises(ValueError): response(json.dumps(obj), "hash")


class Handover(unittest.TestCase):
    def snapshot(self):
        return {"received_ms": 10000,
                "account": {"success": True, "data": {"account_equity": "65.249428",
                    "positions_count": 0, "orders_count": 0, "stop_orders_count": 0}},
                "positions": {"success": True, "data": [], "last_order_id": 123},
                "orders": {"success": True, "data": [], "last_order_id": 123},
                "settings": {"success": True, "data": {"margin_settings": [
                    {"symbol": "SOL", "leverage": 20, "isolated": False}]}}}

    def test_existing_active_writer_is_not_overwritten(self):
        result = handover_readiness(self.snapshot(), self.snapshot(), "active", 10100)
        self.assertFalse(result["flat_baseline_ready"])
        self.assertIn("existing_writer_not_fenced", result["reasons"])

    def test_consistent_flat_state_is_a_baseline_not_authority(self):
        result = handover_readiness(self.snapshot(), self.snapshot(), "inactive", 10100)
        self.assertTrue(result["flat_baseline_ready"])
        self.assertTrue(result["readiness_does_not_authorize_orders"])
        self.assertTrue(result["cross_margin_is_not_a_60_USDC_loss_boundary"])

    def test_external_position_and_stop_orders_block_handover(self):
        for field in ("positions_count", "stop_orders_count", "orders_count"):
            after = self.snapshot(); after["account"]["data"][field] = 1
            self.assertFalse(handover_readiness(self.snapshot(), after, "inactive", 10100)["flat_baseline_ready"])

    def test_nonatomic_or_changed_exchange_sequence_rejected(self):
        after = self.snapshot(); after["orders"]["last_order_id"] = 124
        self.assertFalse(handover_readiness(self.snapshot(), after, "inactive", 10100)["flat_baseline_ready"])
        after["positions"]["last_order_id"] = 124
        self.assertFalse(handover_readiness(self.snapshot(), after, "inactive", 10100)["flat_baseline_ready"])

    def test_failed_or_expired_snapshot_never_becomes_flat(self):
        after = self.snapshot(); after["positions"] = {"success": False, "data": []}
        self.assertFalse(handover_readiness(self.snapshot(), after, "inactive", 10100)["flat_baseline_ready"])
        self.assertFalse(handover_readiness(self.snapshot(), self.snapshot(), "inactive", 13000)["flat_baseline_ready"])


if __name__ == "__main__":
    unittest.main()
