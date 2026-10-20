from concurrent.futures import Future
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'products/arbitrum_research'))
from freezer_pipeline_parallel import OrderedResults, consumer_count


class OrderedTests(unittest.TestCase):
    def test_consumer_bound_preserves_two_and_allows_three_only(self):
        self.assertEqual(consumer_count(2),2);self.assertEqual(consumer_count(3),3)
        for value in (0,1,4,True,'3'):
            with self.assertRaisesRegex(ValueError,'CONSUMER_BOUND'):consumer_count(value)
    def setUp(self):
        self.order = OrderedResults('/retained', 100, 100 + 5 * 16384 - 1)

    def add(self, index):
        n = 100 + index * 16384
        item = dict(start=n, count=16384, folder=f'/retained/window-{n}-{n + 16383}')
        future = Future()
        self.order.admit(item, lambda _: future)
        return future

    def test_later_completion_cannot_jump_gap(self):
        first, second = self.add(0), self.add(1)
        second.set_result('later')
        self.assertIsNone(self.order.ready())
        self.assertEqual(self.order.committed, 0)
        first.set_result('first')
        self.assertEqual(self.order.ready()[1], 'first')
        self.order.commit(dict(first=100, blocks=16384))
        self.assertEqual(self.order.ready()[1], 'later')

    def test_unverified_commit_rejected(self):
        self.add(0)
        with self.assertRaisesRegex(ValueError, 'UNVERIFIED'):
            self.order.commit(dict(first=100, blocks=16384))
        self.assertEqual(self.order.committed, 0)

    def test_failure_stops_even_when_earlier_is_pending(self):
        self.add(0)
        self.add(1).set_exception(ValueError('bad-root'))
        with self.assertRaisesRegex(ValueError, 'bad-root'):
            self.order.ready()
        self.assertEqual(self.order.committed, 0)

    def test_failed_head_never_commits(self):
        self.add(0).set_exception(ValueError('bad-blob'))
        with self.assertRaisesRegex(ValueError, 'bad-blob'):
            self.order.commit(dict(first=100, blocks=16384))
        self.assertEqual(self.order.committed, 0)

    def test_bound_before_launch(self):
        for i in range(4):
            self.add(i)
        with self.assertRaisesRegex(ValueError, 'ADMISSION_BOUND'):
            self.add(4)
        self.assertEqual(self.order.admitted, 4)

    def test_bad_order_not_launched(self):
        with self.assertRaisesRegex(ValueError, 'OUT_OF_ORDER'):
            self.add(1)
        self.assertEqual(self.order.admitted, 0)

    def test_wrong_registration_keeps_cursor(self):
        self.add(0).set_result('verified')
        for wrong in (dict(first=101, blocks=16384), dict(first=100, blocks=1)):
            with self.assertRaisesRegex(ValueError, 'REGISTRATION_SCOPE'):
                self.order.commit(wrong)
        self.assertEqual(self.order.committed, 0)
        self.assertEqual(len(self.order.pending), 1)

    def test_commit_releases_only_one_coordinator_slot(self):
        first = self.add(0)
        for i in range(1, 4):
            self.add(i)
        first.set_result('verified')
        self.order.commit(dict(first=100, blocks=16384))
        self.add(4)
        self.assertEqual(len(self.order.pending), 4)
        self.assertEqual(self.order.committed, 1)

    def test_unsupported_bound_rejected(self):
        with self.assertRaisesRegex(ValueError, 'INFLIGHT_BOUND'):
            OrderedResults('/retained', 1, 10, bound=100)


if __name__ == '__main__':
    unittest.main()
