import struct,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_sbf import Lab
from wire import *
class QuoteSlide(unittest.TestCase):
    def frame(self,l,s,rows,**kwargs):return l.call(s,5,quotes([(k,p,q,l.slot+100,0,False) for k,p,q in rows],slide=True),**kwargs)
    def test_crossing_race_slides_away_and_never_takes_liquidity(self):
        l=Lab();l.quote(0,[(0,14990,100),(8,15001,100)])
        l.quote(1,[(0,15002,100),(8,15010,100)],error=21)
        self.frame(l,1,[(0,15002,100),(8,15010,100)])
        st=l.check();self.assertEqual([(o['slot'],o['price']) for o in st['orders'] if o['seat']==1],[(0,15000),(8,15010)])
        self.assertEqual(st['fees'],0);self.assertTrue(all(s['position']==0 for s in st['seats']))
        self.frame(l,2,[(8,14999,100)])
        self.assertEqual([o['price'] for o in l.check()['orders'] if o['seat']==2],[15001])
    def test_slide_limit_and_price_zero_roll_back_whole_frame(self):
        l=Lab();l.quote(0,[(8,15000,100)])
        self.frame(l,1,[(0,15031,100)]);self.frame(l,1,[(0,15032,100)],error=21)
        l.call(0,7,struct.pack('<Q',65535));l.call(1,7,struct.pack('<Q',65535));l.oracle(1);l.quote(0,[(8,1,1)])
        self.frame(l,1,[(0,1,1)],error=21)
    def test_slid_same_price_order_cannot_jump_fifo(self):
        l=Lab();l.quote(0,[(0,15000,100),(8,15001,100)])
        self.frame(l,1,[(0,15001,100)])
        l.call(2,6,ioc(1,101,15000,minimum=101))
        self.assertEqual([s['position'] for s in l.check()['seats']],[100,1,-101])
    def test_unknown_flags_still_rejected(self):
        l=Lab();p=bytearray(quotes([]));p[1]=4;l.call(0,5,bytes(p),error=3)
if __name__=='__main__':unittest.main(verbosity=2)
