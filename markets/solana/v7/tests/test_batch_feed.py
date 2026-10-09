import asyncio,ctypes as C,json,random,struct,sys,tempfile,time,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('agents','client','muse')]
from wire import *
from feed_batch import Batch,Input,INPUT_BYTES
from compiled import Compiled
from rt_net import Cache
from program import content_hash

def arena(seed):
    rng=random.Random(seed);d=bytearray(SIZE);d[:8]=MAGIC;d[216]=1;wall=int(time.time())
    struct.pack_into('<QQQ',d,144,15000+seed%10,1,seed+1);struct.pack_into('<Q',d,224,wall)
    for s in range(SEATS):
        a=SEAT+s*256;d[a+168]=1;struct.pack_into('<Qq',d,a+112,1_000_000,rng.randrange(-7,8))
        for k in range(16):
            o=NODE+(s*16+k)*48;struct.pack_into('<QQI',d,o,rng.randrange(10),rng.choice([0,2]),rng.randrange(14970,15030));struct.pack_into('<Q',d,o+40,rng.choice([0,wall-1,wall+1]))
    return bytes(d),wall

class BatchFeed(unittest.TestCase):
    def test_compact_event_pump_uses_paired_shared_facts_while_send_is_pending(self):
        from compiled_service import Events
        class Store:
            def __init__(self):self.events=[]
            def event(self,role,kind,body):self.events.append((kind,body))
            def pending(self,role):return ['unresolved existing signature']
        async def exercise(path):
            p=[8,16,5000,20,3,1];proposal={'parameters':p,'program_hash':content_hash(p),'version':1,'execution_authority':False}
            mandate={'ends_unix':time.time()+3,'program_hash':content_hash(p),'compact_feed':True}
            (path/'activation.json').write_text(json.dumps({'enabled':True,'revoked':False,'expires_unix':time.time()+3}))
            cache=Cache(path/'feed',True);store=Store();batch=Batch();data,wall=arena(0);now=time.monotonic_ns()
            cache.publish(data,1,now,batch.prepare(data,1,now,now,wall),wall)
            source=Events(0,path,{},mandate,store,cache,proposal);task=asyncio.create_task(source.pump())
            try:
                for seed in range(1,12):
                    await asyncio.sleep(.01);data,wall=arena(seed);now=time.monotonic_ns()
                    cache.publish(data,1,now,batch.prepare(data,1,now,now,wall),wall)
                await asyncio.sleep(.02);(path/'stop').touch();await task
                decisions=[v for k,v in store.events if k=='compiled_decision']
                self.assertGreaterEqual(len(decisions),8);self.assertTrue(all(v['dispatcher_pending'] for v in decisions))
                self.assertEqual([v for k,v in store.events if k=='compiled_paused'],[])
            finally:
                if not task.done():task.cancel()
                cache.close()
        with tempfile.TemporaryDirectory() as path:asyncio.run(exercise(Path(path)))
    def test_every_seat_excludes_own_best_with_ties_and_expiries(self):
        batch=Batch()
        for seed in range(24):
            d,wall=arena(seed);prepared=batch.prepare(d,1,100,101,wall);st=snapshot(d)
            live=[o for o in st['orders'] if o['expiry']>=1 and (not o['policy_until'] or o['policy_until']>wall)]
            for s in range(SEATS):
                x=Input.from_buffer_copy(prepared[s*INPUT_BYTES:(s+1)*INPUT_BYTES]);other=[o for o in live if o['seat']!=s]
                self.assertEqual(x.bid,max((o['price'] for o in other if o['side']==0),default=0))
                self.assertEqual(x.ask,min((o['price'] for o in other if o['side']==1),default=0))
                buy=sum(o['lots'] for o in live if o['side']==0);sell=sum(o['lots'] for o in live if o['side']==1)
                self.assertEqual(x.depth,buy+sell);self.assertEqual(x.imbalance,(buy-sell)*10000//(buy+sell) if buy+sell else 0)
    def test_deadline_boundary_rescans_and_cache_generation_stays_paired(self):
        d,wall=arena(1);batch=Batch();now=time.monotonic_ns();prepared=batch.prepare(d,1,now,now,wall)
        p=[8,16,5000,20,3,1];proposal={'parameters':p,'program_hash':content_hash(p),'version':1,'execution_authority':False}
        a=Compiled(SEATS-1,now+10**12,proposal,now_ns=now);b=Compiled(SEATS-1,now+10**12,proposal,now_ns=now)
        try:
            x,xf,_=a.decide_bytes(d,1,now,15000,now_ns=now+1,wall=wall+1)
            y,yf,_=b.decide_prepared(d,prepared,wall,1,now,15000,now_ns=now+1,wall=wall+1)
            self.assertEqual(xf,yf);self.assertEqual(x['quotes'],y['quotes'])
            with tempfile.TemporaryDirectory() as path:
                c=Cache(Path(path)/'feed',True);c.publish(d,1,now,prepared,wall)
                out=c.read_prepared_if_changed(0);self.assertEqual(out[0],d);self.assertEqual(out[4],prepared);self.assertEqual(out[5],wall)
                self.assertIsNone(c.read_prepared_if_changed(out[3]));c.close()
        finally:a.close();b.close()
if __name__=='__main__':unittest.main(verbosity=2)
