"""Real SBF fees: cumulative IOC ceiling is invariant to order fragmentation."""
import random,struct,unittest
from test_sbf import Lab
from wire import ioc
class IOCFee(unittest.TestCase):
    def test_whole_order_and_two_counterparties_pay_same_one_unit(self):
        whole=Lab();whole.oracle(1);whole.quote(0,[(8,1,2)])
        whole.call(1,6,ioc(0,2,1,minimum=2));a=whole.check()
        split=Lab();split.oracle(1);split.quote(0,[(8,1,1)]);split.quote(2,[(8,1,1)])
        split.call(1,6,ioc(0,2,1,minimum=2));b=split.check()
        self.assertEqual(a['fees'],1);self.assertEqual(b['fees'],a['fees'])
        self.assertEqual(b['seats'][1]['collateral'],a['seats'][1]['collateral'])
    def test_three_ceiling_boundaries_and_multistep_fills(self):
        for price in (1999,2000,2001):
            l=Lab(9);l.oracle(price)
            for s in range(8):l.quote(s,[(8,price,1)])
            l.call(8,6,ioc(0,8,price,minimum=8,visits=8))
            self.assertEqual(l.check()['fees'],(8*price+1999)//2000)
    def test_independent_iocs_keep_their_own_ceiling(self):
        l=Lab();l.oracle(1);l.quote(0,[(8,1,2)])
        for _ in range(2):l.call(1,6,ioc(0,1,1,minimum=1))
        self.assertEqual(l.check()['fees'],2)
    def test_failed_minimum_rolls_back_all_fills_and_fee(self):
        l=Lab();l.oracle(1);l.quote(0,[(8,1,1)])
        l.call(1,6,ioc(0,2,1,minimum=2),error=24)
        self.assertEqual(l.check()['fees'],0)
    def test_random_partition_fee_difference_matches_exact_integer_reference(self):
        r=random.Random(71001)
        for _ in range(25):
            price=r.randrange(1,16000);quantities=[r.randrange(1,8) for _ in range(8)]
            l=Lab(9);l.oracle(price)
            for s,x in enumerate(quantities):l.quote(s,[(8,price,x)])
            l.call(8,6,ioc(0,sum(quantities),price,minimum=sum(quantities),visits=8))
            st=l.check();self.assertEqual(st['fees'],(sum(quantities)*price+1999)//2000)
            self.assertEqual([s['position'] for s in st['seats']],[-x for x in quantities]+[sum(quantities)])
if __name__=='__main__':unittest.main(verbosity=2)
