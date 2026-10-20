import unittest
from economic_machine.arbitrum_research.history_coverage import intervals, summary


class CoverageTests(unittest.TestCase):
    def test_overlap_and_duplicate_canaries_count_once(self):
        r = summary([(0, 9), (30, 39)], [(5, 14), (5, 14), (40, 49)], 100, 0, 100000)
        self.assertEqual(r['nonoverlapping_indexed_blocks'], 35)
        self.assertEqual(r['overlap_blocks'], 5)
        self.assertEqual(r['merged_ranges'], [[0, 14], [30, 49]])
        self.assertFalse(r['wallet_history_complete'])

    def test_frozen_target_excludes_future_blocks(self):
        self.assertEqual(intervals([(95, 110), (120, 130)], 100), [[95, 99]])

    def test_invalid_ranges_fail_closed(self):
        for rows in [[(-1, 2)], [(5, 4)], [(True, 5)], [(0, 2.5)]]:
            with self.assertRaises(ValueError):
                intervals(rows, 100)

    def test_headroom_is_time_margin(self):
        r = summary([(0, 19)], [], 100, 0, 10000)
        self.assertEqual(r['required_blocks_per_second'], 8)
        self.assertEqual(r['required_with_20_percent_time_margin'], 10)

    def test_new_union_delta_never_subtracts_provider_only_snapshot(self):
        prior = summary([(0, 9)], [(50, 59)], 100, 0, 100000)
        r = summary([(0, 19)], [(50, 69)], 100, 10000, 100000, prior)
        self.assertEqual(r['observed_interval_blocks_per_second'], 2)
        self.assertFalse(r['measured_rate_is_sustained_proof'])

    def test_regression_or_target_change_is_not_zero_progress(self):
        prior = summary([(0, 19)], [], 100, 0, 100000)
        self.assertIsNone(summary([(0, 9)], [], 100, 10000, 100000, prior)['observed_interval_blocks_per_second'])
        self.assertIsNone(summary([(0, 29)], [], 200, 10000, 100000, prior)['observed_interval_blocks_per_second'])

    def test_deadline_expired_keeps_remaining(self):
        r = summary([], [], 100, 1000, 1000)
        self.assertEqual(r['remaining_blocks'], 100)
        self.assertIsNone(r['required_blocks_per_second'])


if __name__ == '__main__':
    unittest.main()
