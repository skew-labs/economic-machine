"""Persistent cache must equal a fresh arbitrary-precision order scan."""
import struct,sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1]/'benchmarks')]
from test_sbf import Lab
from wire import *
from differential_risk import check_cache

class PersistentRisk(unittest.TestCase):
    def check(self,l):l.check();check_cache(l.data())
    def test_partial_rounding_cancel_reduce_reprice_and_self_cleanup(self):
        l=Lab();l.quote(0,[(0,14999,103),(8,15001,103),(9,15003,71)]);self.check(l)
        for lots in (1,2,3,5,11):
            l.call(1,6,ioc(0,lots,15003,minimum=lots));self.check(l)
        l.quote(0,[(0,14999,100),(8,15001,50),(9,15003,70)]);self.check(l)
        l.call(0,7,struct.pack('<Q',1<<8));self.check(l)
        l.call(0,6,ioc(0,1,15003));self.check(l)
        l.quote(0,[(0,14998,99),(8,15004,49)]);self.check(l)
        l.call(2,6,ioc(1,5,14998,minimum=5));self.check(l)
    def test_oracle_change_forces_recompute_even_when_it_returns(self):
        l=Lab();l.quote(0,[(0,14999,100),(8,15001,100)])
        for mark in (15100,14900,15000):
            l.oracle(mark);l.call(1,6,ioc(0,1,15001,minimum=1));self.check(l)
    def test_funding_and_collateral_are_checked_fresh_with_warm_cache(self):
        l=Lab();l.quote(0,[(8,15000,1000),(9,15001,1000)])
        l.call(1,6,ioc(0,1000,15000,minimum=1000));self.check(l)
        l.warp(l.slot+1000);l.oracle(15000)
        l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),11,payload=struct.pack('<q',-10))]);self.check(l)
        l.call(0,4,struct.pack('<Q',999_000_000),error=12);self.check(l)
    def test_failed_multi_fill_keeps_cache_and_all_other_bytes_unchanged(self):
        l=Lab();l.quote(0,[(8,15000,1000),(9,15001,1000)])
        l.call(1,4,struct.pack('<Q',999_990_000));before=l.data()
        l.call(1,6,ioc(0,2000,15001,minimum=2000),error=12)
        self.assertEqual(before,l.data());self.check(l)
    def test_clipped_band_boundaries_and_deleted_extrema_keep_exact_margin(self):
        for mark in (1,14299,15000,15778,0xffffff):
            for cancel in (False,True):
                l=Lab();l.quote(0,[(0,14980,101),(1,14990,103),(8,15010,107),(9,15020,109)])
                if cancel:l.call(0,7,struct.pack('<Q',(1<<0)|(1<<9)))
                l.oracle(mark);st=l.check();orders=st['orders'];bids=sum(o['lots'] for o in orders if o['side']==0);asks=sum(o['lots'] for o in orders if o['side']==1)
                loss=sum(o['lots']*max(0,o['price']-mark*95//100 if o['side']==0 else (mark*105+99)//100-o['price'])+(o['lots']*o['price']+1999)//2000 for o in orders)
                reserve=(max(bids,asks)*mark+9)//10+loss
                if reserve>1_000_000_000:
                    l.call(0,4,struct.pack('<Q',1),error=12)
                else:
                    l.call(0,4,struct.pack('<Q',1_000_000_000-reserve+1),error=12)
                    l.call(0,4,struct.pack('<Q',1_000_000_000-reserve))
                self.check(l)

if __name__=='__main__':unittest.main(verbosity=2)
