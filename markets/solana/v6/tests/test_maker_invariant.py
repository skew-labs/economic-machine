"""Independent integer model of the removed redundant maker post-fill check."""
import random,unittest
class MakerInvariant(unittest.TestCase):
    def test_two_hundred_thousand_integer_boundaries(self):
        r=random.Random(61009)
        for n in range(200000):
            mark=r.choice((1,1999,2000,15000,0xffffff,r.randrange(1,0x1000000)))
            tick=r.choice((1,1000000,r.randrange(1,1000001)))
            q=r.randrange(-1000000,1000001);bids=r.randrange(1000000-q+1);asks=r.randrange(1000000+q+1)
            side=n%2;available=bids if side==0 else asks
            if not available:continue
            before=r.randrange(1,min(available,1000000)+1);fill=r.randrange(1,before+1);after=before-fill
            price=r.choice((1,0xffffff,mark,max(1,mark*95//100),min(0xffffff,(mark*105+99)//100)))
            gap=max(0,price-mark*95//100 if side==0 else (mark*105+99)//100-price)
            loss=lambda x:x*gap*tick+(x*price*tick+1999)//2000
            oldloss=loss(before);newloss=loss(after);q2=q+(fill if side==0 else -fill)
            b2=bids-fill if side==0 else bids;a2=asks if side==0 else asks-fill
            worst=max(abs(q),abs(q+bids),abs(q-asks));worst2=max(abs(q2),abs(q2+b2),abs(q2-a2))
            im=(worst*mark*tick+9)//10;im2=(worst2*mark*tick+9)//10
            floor=im+oldloss;coll=floor+r.randrange(2001);eq=floor+r.randrange(4001)
            eq2=eq+fill*(mark-price if side==0 else price-mark)*tick
            self.assertLessEqual(worst2,worst)
            self.assertGreaterEqual(min(eq2,coll)-im2-newloss,min(eq,coll)-im-oldloss)
if __name__=='__main__':unittest.main(verbosity=2)
