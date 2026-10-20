import unittest
from economic_machine.pump_research.workload_inventory import (
    GIB, parse_report, rounded_gib_bounds, estimate, interval_rate)


def row(epoch, size='4 GB'):
    url=f'https://files.old-faithful.net/{epoch}/epoch-{epoch}.car'
    return f'| {epoch} | [car]({url}) | sha | b3 | {size} | check | poh | index | 6 GB | ranges | slots |'


class Inventory(unittest.TestCase):
    def test_binary_units_despite_label(self):
        v=rounded_gib_bounds('4 GB')
        self.assertEqual(v['lower_bytes'], 7*GIB//2)
        self.assertEqual(v['upper_bytes'], 9*GIB//2)

    def test_minimum_rounding(self):
        self.assertEqual(rounded_gib_bounds('1 GB')['lower_bytes'], 1)

    def test_missing_not_zero(self):
        rows=parse_report((row(0)+'\n|1|ongoing|-|-|-|-|-|-|-|-|-|\n|2|❌|❌|❌|❌|❌|❌|❌|❌|❌|❌|').encode())
        v=estimate(rows,available_seconds=10,cloud_cap_bytes=GIB)
        self.assertEqual(v['ongoing_epochs'], [1]);self.assertEqual(v['missing_epochs'], [2])
        self.assertIsNone(v['remaining_transactions']);self.assertFalse(v['exact_all_car_bytes_known'])

    def test_duplicate_rejected(self):
        with self.assertRaisesRegex(ValueError, 'EPOCH'):parse_report((row(0)+'\n'+row(0)).encode())

    def test_source_substitution_rejected(self):
        with self.assertRaisesRegex(ValueError, 'SOURCE'):parse_report(row(0).replace('old-faithful.net','evil.example').encode())

    def test_head_disagreement(self):
        rows=parse_report(row(0).encode())
        with self.assertRaisesRegex(ValueError,'DISAGREEMENT'):
            estimate(rows,known_exact=[dict(epoch=0,bytes=7*GIB,url=rows[0]['car_url'])],available_seconds=10,cloud_cap_bytes=GIB)

    def test_exact_head_replaces_interval(self):
        rows=parse_report(row(0).encode());size=4*GIB+123
        v=estimate(rows,known_exact=[dict(epoch=0,bytes=size,url=rows[0]['car_url'])],available_seconds=10,cloud_cap_bytes=GIB)
        self.assertEqual(v['car_bytes_lower'],size);self.assertEqual(v['car_bytes_upper'],size)
        self.assertEqual(v['full_car_bytes_per_second_with_20pct_time_headroom'],size/8)

    def test_inventory_is_gross_not_remaining(self):
        v=estimate(parse_report(row(0).encode()),available_seconds=10,cloud_cap_bytes=GIB)
        self.assertTrue(v['full_car_gross_bytes_not_remaining']);self.assertIsNone(v['derived_graph_bytes'])

    def test_unknown_label(self):
        self.assertIsNone(rounded_gib_bounds('❌'))
        with self.assertRaisesRegex(ValueError,'SIZE_MISSING'):parse_report(row(0,'?').encode())

    def test_interval_rate(self):
        a=dict(epoch=1,total_bytes=1000,received_bytes=10,at_ms=1000)
        b=dict(a,received_bytes=40,at_ms=11000)
        self.assertEqual(interval_rate(a,b)['bytes_per_second'],3)
        with self.assertRaisesRegex(ValueError,'DIFFERENT_OBJECT'):interval_rate(a,dict(b,epoch=2))
        with self.assertRaisesRegex(ValueError,'ORDER'):interval_rate(b,a)


if __name__=='__main__':unittest.main()
