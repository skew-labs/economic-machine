import asyncio,json,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agents'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'muse'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
from compiled import Compiled
from program import content_hash,parameters,request,validate,MODEL
from contract import Rejected
from compiled_service import Events
P=[4,12,5000,30,4,2]
def proposal(p=P):return {'parameters':p,'program_hash':content_hash(p),'version':1,'execution_authority':False}
def state(q=0):return {'oracle':15000,'oracle_slot':1,'market_sequence':1,'seats':[{'position':q}],'orders':[]}
class ConditionalPrograms(unittest.TestCase):
    def test_model_and_format_are_pinned_and_no_market_or_credentials_sent(self):
        r=request();self.assertEqual(r['model'],'meta/muse-spark-1.3-contributor');self.assertLessEqual(len(json.dumps(r,separators=(',',':')).encode()),2048)
        self.assertEqual(r['provider']['only'],['meta']);self.assertFalse(r['provider']['allow_fallbacks'])
        v={'model':MODEL,'provider':'Meta','choices':[{'finish_reason':'stop','message':{'content':json.dumps({'p':P})}}]}
        self.assertEqual(validate(v,1)['parameters'],P)
        for bad in ({'p':[4,12,5000,30,4,True]},{'p':[4,12,5000,30,4,3]},{'p':[12,4,5000,30,4,2]},{'p':P,'orders':[]}):
            with self.assertRaises(Rejected):parameters(bad)
        with self.assertRaises(Rejected):validate(v,30)
    def test_same_installed_program_changes_modes_without_a_model(self):
        c=Compiled(0,10**12,proposal(),now_ns=1);st=state()
        for now,f,mode in [(10,{'imbalance':0,'movement':0,'depth':8},2),(20,{'imbalance':6000,'movement':0,'depth':8},3),(30,{'imbalance':0,'movement':40,'depth':8},3),(40,{'imbalance':0,'movement':0,'depth':0},3),(50,{'imbalance':0,'movement':0,'depth':8},2)]:
            d=c.decide(st,1,now,f,now_ns=now+1);self.assertEqual(d['mode'],mode);self.assertEqual(d['program_version'],1);c.checkpoint(d)
        st['seats'][0]['position']=6;d=c.decide(st,1,60,{'imbalance':0,'movement':0,'depth':8},now_ns=61)
        self.assertTrue(d['reduce']);self.assertTrue(all(q['slot']>=8 for q in d['quotes']));c.close()
    def test_facts_and_program_expire_independently(self):
        c=Compiled(0,10**12,proposal(),now_ns=1);f={'imbalance':0,'movement':0,'depth':8}
        with self.assertRaises(RuntimeError):c.decide(state(),1,10,f,now_ns=3_000_000_011)
        c.close();c=Compiled(0,100,proposal(),now_ns=1)
        with self.assertRaises(RuntimeError):c.decide(state(),1,99,f,now_ns=100)
        c.close()
    def test_invalid_program_identity_cannot_install(self):
        p=proposal();p['parameters']=[4,12,5000,30,4,1]
        with self.assertRaises(RuntimeError):Compiled(0,100,p,now_ns=1)
    def test_event_producer_continues_while_dispatch_is_pending(self):
        import compiled_service
        class Cache:
            def __init__(self):self.n=0
            def read_if_changed(self,prior):
                self.n+=1;st=state();st['market_sequence']=self.n
                return st,1,time.monotonic_ns(),self.n
        class Store:
            def __init__(self):self.events=[]
            def event(self,role,kind,body):self.events.append((kind,body))
            def pending(self,role):return ['slow RPC']
        async def exercise(private):
            store=Store();m={'ends_unix':time.time()+1,'program_hash':content_hash(P)}
            e=Events(0,private,{},m,store,Cache(),proposal());task=asyncio.create_task(e.pump())
            await asyncio.sleep(.08);(private/'stop').touch();await task
            ds=[x[1] for x in store.events if x[0]=='compiled_decision'];self.assertGreaterEqual(len(ds),3);self.assertTrue(all(x['dispatcher_pending'] for x in ds))
        old=compiled_service.market_ok;compiled_service.market_ok=lambda *args:None
        try:
            with tempfile.TemporaryDirectory() as d:
                p=Path(d);(p/'activation.json').write_text(json.dumps({'enabled':True,'revoked':False,'expires_unix':time.time()+10}));asyncio.run(exercise(p))
        finally:compiled_service.market_ok=old
if __name__=='__main__':unittest.main(verbosity=2)
