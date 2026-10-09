import asyncio,json,struct,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agents'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'muse'))
from rt_store import Store
from rt_net import Cache
from rt_tx import Tx
from native import Native
from controller import request,public_features,bounded_response
from wire import SIZE
class Operations(unittest.TestCase):
    def test_expired_or_near_expiry_session_is_not_signed_or_sent(self):
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        class NoNet:
            async def call(self,*args):raise AssertionError('expired session must not fetch a blockhash or submit')
        with tempfile.TemporaryDirectory() as d:
            mandate={'max_transactions':3,'actor_transaction_limit':3,'ends_unix':time.time()+100,'boot_id':'test'}
            store=Store(Path(d)/'r.sqlite',mandate);key=Keypair()
            tx=Tx(0,key,{'program':str(Pubkey.new_unique()),'market':str(Pubkey.new_unique())},mandate,NoNet(),store)
            st={'read_slot':1000,'market_sequence':1,'seats':[{'session':str(key.pubkey()),'session_limited':True,'epoch':2,'sequence':1,'position':0}]}
            for expiry in (999,1000,1030,1060):
                st['seats'][0]['session_expiry']=expiry
                with self.assertRaisesRegex(RuntimeError,'session expired'):asyncio.run(tx.send(st,5,b'',source_ns=time.monotonic_ns()))
            self.assertEqual(store.rows(),[])
    def test_durable_journal_delay_cancels_before_first_broadcast(self):
        from unittest.mock import patch
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.hash import Hash
        class Net:
            async def call(self,*args):raise AssertionError('stale signature must never be broadcast')
        with tempfile.TemporaryDirectory() as d:
            mandate={'max_transactions':3,'actor_transaction_limit':3,'ends_unix':time.time()+100,'boot_id':'test'}
            store=Store(Path(d)/'r.sqlite',mandate);key=Keypair()
            tx=Tx(0,key,{'program':str(Pubkey.new_unique()),'market':str(Pubkey.new_unique())},mandate,Net(),store)
            tx.blockhash={'blockhash':str(Hash.default()),'lastValidBlockHeight':100};tx.blockhash_at=time.monotonic()
            clock=[10];prepare=store.prepare
            def delayed(*args):prepare(*args);clock[0]=4_000_000_000
            store.prepare=delayed
            st={'read_slot':1,'market_sequence':1,'seats':[{'session':str(key.pubkey()),'session_limited':True,'session_expiry':1000,'epoch':2,'sequence':1,'position':0}]}
            with patch('rt_tx.time.monotonic_ns',side_effect=lambda:clock[0]):result=asyncio.run(tx.send(st,5,b'',source_ns=1))
            self.assertIsNone(result);self.assertEqual(store.rows()[0]['state'],'CANCELLED');self.assertFalse(store.pending(0))
    def test_blockhash_network_wait_cannot_dispatch_expired_source(self):
        from unittest.mock import patch
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.hash import Hash
        class DelayedNet:
            async def call(self,method,params):
                self.method=method
                return {'value':{'blockhash':str(Hash.default()),'lastValidBlockHeight':100}}
        with tempfile.TemporaryDirectory() as d:
            mandate={'max_transactions':3,'actor_transaction_limit':3,'ends_unix':time.time()+100,'boot_id':'test'}
            store=Store(Path(d)/'r.sqlite',mandate);key=Keypair();net=DelayedNet()
            tx=Tx(0,key,{'program':str(Pubkey.new_unique()),'market':str(Pubkey.new_unique())},mandate,net,store)
            st={'read_slot':1,'market_sequence':1,'seats':[{'session':str(key.pubkey()),'session_limited':True,'session_expiry':1000,'epoch':2,'sequence':1}]}
            with patch('rt_tx.time.monotonic_ns',return_value=4_000_000_000):
                with self.assertRaisesRegex(RuntimeError,'source expired'):asyncio.run(tx.send(st,5,b'',source_ns=1))
            self.assertEqual(net.method,'getLatestBlockhash');self.assertEqual(store.rows(),[])
    def test_failed_revocation_requires_finalized_expiry_before_replacement(self):
        from journal import Journal
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'j.sqlite',{'max_transactions':3,'gross_turnover_limit':10})
            j.prepare('revoke-0','old','raw',0,{})
            receipt={'slot':101,'transaction':{'signatures':['old']},'meta':{'err':{'InstructionError':[1,{'Custom':8}]}}}
            proof={'confirmation_status':'confirmed','slot':101,'expiry_slot':100}
            with self.assertRaises(RuntimeError):j.retire_failed_revocation('revoke-0',receipt,proof)
            with self.assertRaises(RuntimeError):j.prepare('revoke-1','new','raw',0,{})
            proof['confirmation_status']='finalized';j.retire_failed_revocation('revoke-0',receipt,proof)
            self.assertIsNone(j.get('revoke-0'));self.assertEqual(j.rows()[0]['state'],'FAILED_FINALIZED')
            j.prepare('revoke-0','replacement','raw',0,{})
            self.assertEqual(len(j.rows()),2)
    def test_provider_response_can_arrive_in_multiple_chunks_and_is_bounded(self):
        class Chunks:
            def __init__(self,parts):self.parts=parts
            async def iter_chunked(self,n):
                for part in self.parts:yield part
        self.assertEqual(asyncio.run(bounded_response(Chunks([b'{"c":',b'2}']))),{'c':2})
        with self.assertRaises(RuntimeError):asyncio.run(bounded_response(Chunks([b' '*65536,b'x'])))
    def test_stale_actor_sequence_cannot_be_resigned_as_new_intent(self):
        from solders.keypair import Keypair
        with tempfile.TemporaryDirectory() as d:
            mandate={'max_transactions':3,'actor_transaction_limit':3,'ends_unix':time.time()+10}
            store=Store(Path(d)/'r.sqlite',mandate);store.prepare(0,'prior','raw',{'epoch':2,'sequence':8});store.update('prior','CONFIRMED',{'epoch':2,'sequence':8})
            tx=Tx.__new__(Tx);tx.role=0;tx.key=Keypair();tx.store=store;tx.mandate=mandate;tx.manifest={}
            st={'seats':[{'session':str(tx.key.pubkey()),'session_limited':True,'epoch':2,'sequence':7}]}
            with self.assertRaisesRegex(RuntimeError,'caught up'):asyncio.run(tx.send(st,5,b''))
            self.assertEqual(len(store.rows()),1)
    def test_independent_pending_and_persistent_limits(self):
        with tempfile.TemporaryDirectory() as d:
            m={'max_transactions':3,'actor_transaction_limit':2,'ends_unix':time.time()+10};path=Path(d)/'runtime.sqlite';a=Store(path,m);b=Store(path,m)
            a.prepare(0,'sig0','raw',{'op':5});b.prepare(1,'sig1','raw',{'op':6})
            with self.assertRaises(RuntimeError):b.prepare(0,'duplicate','raw',{})
            a.update('sig0','CONFIRMED',{'op':5});b.prepare(0,'sig2','raw',{'op':7});a.db.close();b.db.close()
            c=Store(path,m);self.assertEqual(len(c.rows()),3)
            self.assertEqual(c.get('sig0')['state'],'CONFIRMED');self.assertIsNone(c.get('absent'))
            self.assertEqual([c.count_op(0,op) for op in (5,6,7)],[1,0,1]);self.assertEqual(c.count_op(1,6),1)
            with self.assertRaises(RuntimeError):c.prepare(2,'sig3','raw',{})
    def test_cache_rejects_incomplete_stale_and_backwards_feed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'cache';w=Cache(p,True);r=Cache(p)
            with self.assertRaises(RuntimeError):r.read()
            data=bytes(SIZE);now=time.monotonic_ns();w.publish(data,5,now)
            self.assertEqual(r.read()[1],5);self.assertFalse(w.publish(data,4,now))
            struct.pack_into('<Q',w.map,16,now-4_000_000_000)
            with self.assertRaises(RuntimeError):r.read()
            prior=struct.unpack_from('<Q',w.map,0)[0];w.close();w=Cache(p,True)
            self.assertGreater(struct.unpack_from('<Q',w.map,0)[0],prior)
            with self.assertRaises(RuntimeError):r.read()
            w.publish(data,6,time.monotonic_ns());self.assertEqual(r.read()[1],6);r.close();w.close()
    def test_native_policy_and_long_running_checkpoint(self):
        n=Native(0,1_000_000_000,now_ns=1)
        st={'oracle':15000,'oracle_slot':1,'market_sequence':1,'orders':[],'seats':[{'position':0}]}
        for i in range(600):
            now=i+10;n.policy(3,now+10,now);d=n.decide(st,1,now,now_ns=now+1);self.assertEqual(len(d['quotes']),2);n.checkpoint(d)
        n.policy(0,1000,now_ns=700)
        with self.assertRaises(RuntimeError):n.decide(st,1,701,now_ns=702)
        n.policy(2,705,now_ns=703)
        with self.assertRaises(RuntimeError):n.decide(st,1,705,now_ns=706)
        n.close()
    def test_public_projection_cannot_carry_inventory_credentials_or_addresses(self):
        st={'read_slot':1,'seats':[{'position':999,'secret':'do-not-send'}],'orders':[{'seat':0,'side':0,'price':100,'lots':2,'expiry':100,'policy_until':0},{'seat':1,'side':1,'price':104,'lots':2,'expiry':100,'policy_until':0}]}
        features=public_features(st);raw=json.dumps(request(features))
        self.assertLessEqual(len(raw.encode()),2048)
        for forbidden in ('do-not-send','position','999','seats','private_key'):self.assertNotIn(forbidden,raw)
        self.assertEqual(features,{'s':4,'i':0,'v':0,'d':[4,4,4]})
        with self.assertRaises(ValueError):request(dict(features,secret='bad'))
    def test_expired_unknown_requires_unchanged_finalized_sequence(self):
        class FakeNet:
            async def call(self,method,params):
                return {'value':[None]} if method=='getSignatureStatuses' else 101 if method=='getBlockHeight' else 200
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'r.sqlite',{'max_transactions':3,'actor_transaction_limit':3,'ends_unix':time.time()+10})
            body={'last_valid_height':100,'epoch':2,'sequence':5,'command_digest':'abc','op':5}
            store.prepare(0,'sig','raw',body)
            tx=Tx.__new__(Tx);tx.role=0;tx.store=store;tx.net=FakeNet();tx.manifest={}
            async def current(*args):return {'seats':[{'epoch':2,'sequence':4}]},200,0
            tx.current=current;asyncio.run(tx.resolve(store.rows()[0]));self.assertEqual(store.rows()[0]['state'],'EXPIRED')
    def test_receipt_partial_fill_is_recorded_without_full_fill_assumption(self):
        import base64
        class FakeNet:
            async def call(self,method,params):
                if method=='getSignatureStatuses':return {'value':[{'err':None,'confirmationStatus':'finalized'}]}
                return {'transaction':{'signatures':['sig']},'meta':{'err':None,'returnData':{'programId':'p','data':[base64.b64encode(struct.pack('<QQ',1,15000)).decode(),'base64']}}}
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'r.sqlite',{'max_transactions':3,'actor_transaction_limit':3,'ends_unix':time.time()+10})
            body={'last_valid_height':100,'epoch':2,'sequence':5,'command_digest':'abc','op':6,'max_lots':2,'turnover_cap':30000};store.prepare(2,'sig','raw',body)
            tx=Tx.__new__(Tx);tx.role=2;tx.store=store;tx.net=FakeNet();tx.program='p'
            asyncio.run(tx.resolve(store.rows()[0]));self.assertEqual(json.loads(store.rows()[0]['body'])['filled_lots'],1)
if __name__=='__main__':unittest.main(verbosity=2)
