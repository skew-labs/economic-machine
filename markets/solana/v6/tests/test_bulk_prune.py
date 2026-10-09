import struct,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_sbf import Lab
from wire import *
class BulkPrune(unittest.TestCase):
    def test_only_unfunded_orders_removed_and_survivor_fifo_unchanged(self):
        l=Lab(16)
        for s in range(16):l.quote(s,[(k,14990-k if k<8 else 15010+k-8,1 if s in (5,11) else 1000) for k in range(16)])
        before=l.check();l.oracle(0xffffff);l.settle();after=l.check()
        self.assertEqual({o['seat'] for o in after['orders']},{5,11});self.assertEqual(len(after['orders']),32)
        self.assertEqual(after['fees'],before['fees']);self.assertEqual([s['collateral'] for s in after['seats']],[s['collateral'] for s in before['seats']])
        l.oracle(15000);l.call(0,6,ioc(0,3,15011,minimum=3,visits=3));st=l.check()
        self.assertEqual([st['seats'][s]['position'] for s in (0,5,11)],[3,-2,-1])
        for s in (5,11):l.call(s,7,struct.pack('<Q',65535))
        self.assertFalse(l.check()['orders'])
        l.quote(1,[(0,14999,1),(8,15001,1)]);self.assertEqual(len(l.check()['orders']),2)
    def test_all_invalid_uses_full_clear_without_changing_ledger(self):
        l=Lab(16)
        for s in range(16):l.quote(s,[(k,14990-k if k<8 else 15010+k-8,1000) for k in range(16)])
        l.oracle(0xffffff);l.settle();st=l.check();self.assertFalse(st['orders']);self.assertEqual(sum(s['collateral'] for s in st['seats']),16*10**9)
        self.assertFalse(any(l.data()[NODE:SIZE]))
if __name__=='__main__':unittest.main(verbosity=2)
