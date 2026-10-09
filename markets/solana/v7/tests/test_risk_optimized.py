"""Cases that stress incremental maker risk, including cache invalidation."""
import struct,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_sbf import Lab
from wire import *

class RiskOptimized(unittest.TestCase):
    def test_maximum_price_quantity_tick_value_has_exact_fee_and_margin(self):
        l=Lab(2,tick_value=1_000_000,collateral=8_000_000_000_000_000_000)
        price=0xffffff;quantity=1_000_000;notional=price*quantity*1_000_000
        l.oracle(price);l.quote(0,[(8,price,quantity)])
        l.call(1,6,ioc(0,quantity,price,minimum=quantity))
        st=l.check();self.assertEqual(st['fees'],(notional*5+9999)//10000)
        self.assertEqual(st['seats'][0]['quote'],notional)
        self.assertEqual(st['seats'][1]['quote'],-notional)
        self.assertGreater(notional,2**63-1);self.assertLess(notional+1999,2**64)
        l.quote(0,[(8,price,1)],error=11)
        reserve=(notional+9)//10
        l.call(0,4,struct.pack('<Q',st['seats'][0]['collateral']-reserve));l.check()
    def test_cached_maker_expired_middle_node_and_partial_fill(self):
        l=Lab();l.call(0,5,quotes([(8,15000,101,l.slot+100,0,False),(9,15001,101,l.slot+1,0,False),(10,15002,103,l.slot+100,0,False)]))
        l.warp(l.slot+2);l.oracle(15000)
        l.call(1,6,ioc(0,151,15002,minimum=151,visits=3))
        st=l.check();self.assertEqual(st['seats'][0]['position'],-151)
        self.assertEqual([(o['slot'],o['lots']) for o in st['orders']],[(10,53)])
        self.assertEqual(st['fees'],(101*15000+50*15002+1999)//2000)
    def test_full_capacity_match_cancel_and_reuse(self):
        l=Lab(SEATS)
        for s in range(SEATS):l.quote(s,[(k,14990-k if k<8 else 15010+k-8,100) for k in range(16)])
        self.assertEqual(len(l.check()['orders']),SEATS*16)
        l.call(SEATS-1,6,ioc(0,1500,15010,minimum=1500,visits=16))
        self.assertEqual(l.check()['seats'][SEATS-1]['position'],1500)
        for s in range(SEATS):l.call(s,7,struct.pack('<Q',65535))
        self.assertFalse(l.check()['orders'])
        for s in range(SEATS):l.quote(s,[(0,14990,1),(8,15010,1)])
        self.assertEqual(len(l.check()['orders']),SEATS*2)
    def test_margin_rejection_rolls_back_multiple_fills(self):
        l=Lab();l.call(1,4,struct.pack('<Q',999_500_000))
        l.quote(0,[(8+i,15000+i,1000) for i in range(8)])
        before=l.data();l.call(1,6,ioc(0,8000,15007,minimum=8000,visits=8),error=12)
        self.assertEqual(l.data(),before);l.check()
    def test_remaining_maker_orders_keep_their_margin_requirement(self):
        l=Lab();l.quote(0,[(0,14990,1000),(8,15000,1000),(9,15001,1000)])
        l.call(1,6,ioc(0,1500,15001,minimum=1500,visits=2))
        s=l.check()['seats'][0]
        # Independent arbitrary-precision reference for collateral withdrawal.
        q=s['position'];mark=15000;orders=[o for o in l.check()['orders'] if o['seat']==0]
        bids=sum(o['lots'] for o in orders if o['side']==0);asks=sum(o['lots'] for o in orders if o['side']==1)
        loss=sum(o['lots']*max(0,o['price']-mark*95//100 if o['side']==0 else (mark*105+99)//100-o['price'])+(o['lots']*o['price']*5+9999)//10000 for o in orders)
        minimum=(max(abs(q),abs(q+bids),abs(q-asks))*mark+9)//10+loss
        available=s['collateral']-minimum+min(0,s['equity']-s['collateral'])
        l.call(0,4,struct.pack('<Q',available+1),error=12)
        l.call(0,4,struct.pack('<Q',available));l.check()

if __name__=='__main__':unittest.main(verbosity=2)
