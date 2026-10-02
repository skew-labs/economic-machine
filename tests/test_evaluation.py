import unittest

from machine_commerce.evaluation import (
    final_json,
    paired_outcomes,
    saving,
    timely,
    usage_record,
    usage_sum,
    wilson,
)


class EvaluationTests(unittest.TestCase):
    def test_missing_usage_is_not_free_including_failed_attempts(self):
        rows = [{"usage": usage_record({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": ".01"})},
                {"usage": usage_record(None)}]
        total = usage_sum(rows)
        self.assertEqual(total["calls"], 2)
        self.assertIsNone(total["total_tokens"])
        self.assertIsNone(total["cost_usd"])
        self.assertIsNone(saving(total["total_tokens"], 12))

    def test_exact_decimal_cost_and_startup_inclusive_savings(self):
        startup = {"usage": usage_record({"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50, "cost": ".001"})}
        hot = {"usage": usage_record({"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100, "cost": ".003"})}
        self.assertEqual(usage_sum([startup, hot])["cost_usd"], "0.004")
        self.assertAlmostEqual(saving(usage_sum([startup, hot])["total_tokens"], 50), 2 / 3)
        self.assertIsNone(saving(0, 0))

    def test_invalid_usage_is_not_silently_counted(self):
        for raw in [{"prompt_tokens": True}, {"cost": "NaN"}, {"cost": "-1"},
                    {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 10}]:
            self.assertIsNone(usage_record(raw)["total_tokens"])
        self.assertIsNone(usage_record({"cost": "Infinity"})["cost_usd"])

    def test_empty_hot_path_is_known_zero(self):
        self.assertEqual(usage_sum([])["total_tokens"], 0)
        self.assertEqual(usage_sum([])["cost_usd"], "0")

    def test_late_valid_agreement_does_not_count_as_success(self):
        self.assertTrue(timely(True, 5000, 5))
        self.assertFalse(timely(True, 5000.01, 5))
        self.assertFalse(timely(False, 1, 5))

    def test_paired_exact_test_and_no_improvement_baseline(self):
        result = paired_outcomes([(False, True)] * 10)
        self.assertEqual(result["machine_only_success"], 10)
        self.assertEqual(result["exact_mcnemar_two_sided_p"], 2 / 1024)
        self.assertEqual(paired_outcomes([(True, True)] * 10)["rate_difference"], 0)
        self.assertEqual(paired_outcomes([])["exact_mcnemar_two_sided_p"], 1)

    def test_intervals_do_not_claim_population_certainty(self):
        interval = wilson(24, 24)
        self.assertLess(interval[0], .9)
        self.assertAlmostEqual(interval[1], 1)
        self.assertIsNone(wilson(0, 0))

    def test_only_final_json_can_be_executed(self):
        self.assertEqual(final_json('<think>untrusted thought</think>{"seller_id":null}'), {"seller_id": None})
        self.assertEqual(final_json('```json\n{"seller_id":"seller-4"}\n```'), {"seller_id": "seller-4"})
        for content in [None, "", "[]", 'explanation {"seller_id":null}', '```json invalid']:
            with self.assertRaises((ValueError, TypeError)):
                final_json(content)
