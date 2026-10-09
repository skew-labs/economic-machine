import copy,ctypes as C,json,os,random,sqlite3,sys,tempfile,time,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from bridge import *
from schema import profile,seed,BOUNDS
from preferences import personalize
from runtime import Engine as ReferenceEngine
from evaluate import replay as reference_replay
from test_evolution import synthetic
ROOT=Path(__file__).resolve().parents[1]
OUT=Path(os.environ.get('MP_RESEARCH_EVIDENCE',str(ROOT/'evidence/native-research')));OUT.mkdir(parents=True,exist_ok=True)
os.environ.setdefault('MP_LAYOUT','wide256')
os.environ.setdefault('MP_ELF',str(ROOT/'build/sbf/machine_perps.so'))

def fixture():
    p=profile('test-owner','balanced');f,r=synthetic();w=Window(f,r)
    return p,seed(p),w,w.fence(0,999*10**9)
def candidates():
    return json.loads((Path(__file__).parent/'fixtures/strategies.json').read_text())

class NativeTests(unittest.TestCase):
    def test_all_14553_joint_output_encodings_are_lossless(self):
        p=profile('encoding','active');s={'nodes':[[1,0,0,0,i] for i in (2,3,4,5)],'outputs':[0,1,2,3]};now=1
        with Engine(s,p) as e:
            for skew in range(-24,25):
                for spread in range(33):
                    for clip in range(3):
                        for action in range(3):
                            now+=1;result=e.decide([0,0,skew,spread,clip,action],15000,now=now)
                            self.assertEqual(result['raw'],[skew,spread,clip,action])
        self.assertEqual(now-1,14553)
    def test_quote_and_risk_parity_all_muse_candidates(self):
        r=random.Random(914);total=0
        for program in candidates():
            for style in ('defensive','balanced','active'):
                p=profile('parity',style)
                with Engine(program,p) as n,ReferenceEngine(program,p) as old:
                    for i in range(100):
                        f=[r.randint(*b) for b in BOUNDS];mark=15000+r.randint(-50,50);bid=mark-2;ask=mark+2
                        a=n.decide(f,mark,bid,ask,i+2);b=old.decide(f,mark,bid,ask,i+2)
                        for key in ('quotes','reduce','raw'):self.assertEqual(a[key],b[key]);total+=1
        self.assertEqual(total,5400)
    def test_replay_metrics_and_trace_match_python(self):
        p,s,w,fence=fixture();programs=[s]+candidates()
        for program in programs:
            for delay,queue in ((1,1),(3,2)):
                old=reference_replay(program,p,w.source,w.receipts,delay,queue)
                m,trace,_=replay(program,p,w,fence,delay,queue,with_trace=True);new=report(m)
                for key in new:
                    if key in old:self.assertAlmostEqual(new[key],old[key],places=5,msg=key)
                self.assertEqual(trace,old['trace'])
    def test_postproposal_purge_consumption_and_order_fences(self):
        p,s,w,b=fixture()
        cases=[w.fence(1,0),w.fence(0,w.frames[0].unix_ns),w.fence(0,0,consumed=1),w.fence(0,0,purge=1)]
        for fence in cases:
            with self.assertRaises(ValueError):replay(s,p,w,fence)
        for field,value in [('slot',w.frames[0].slot),('id',w.frames[0].id),('unix_ns',w.frames[0].unix_ns+16*10**9),('verified',0),('external',1),('provenance',0),('publish_ns',1),('funding',-(2**63)),('order_count',2**64-1)]:
            original=getattr(w.frames[1],field);setattr(w.frames[1],field,value)
            with self.assertRaises(ValueError):replay(s,p,w,b)
            setattr(w.frames[1],field,original)
    def test_costs_are_charged_and_can_stop_trading(self):
        p,s,w,b=fixture();plain,_,_=replay(s,p,w,b)
        costs=w.fence(0,999*10**9,quote_cost=100,exit_cost=30,maker_fee_bps=5);charged,_,_=replay(s,p,w,costs)
        self.assertGreater(charged.network_cost,0);self.assertGreaterEqual(charged.maker_fees,0);self.assertLess(charged.pnl,plain.pnl);self.assertTrue(charged.loss_stop)
    def test_same_policy_and_no_fill_cannot_win(self):
        p,s,w,b=fixture();result,_=compare(s,s,p,w,b);self.assertFalse(result.eligible)
        pause={'nodes':[[2,0,0,0,2]],'outputs':[0,0,0,0]};result,_=compare(s,pause,p,w,b);self.assertFalse(result.eligible)
        with self.assertRaises(ValueError):compare(s,s,p,w,w.fence(0,0,training=True))
    def test_no_future_feature_leak(self):
        p,s,w,b=fixture();_,first,_=replay(s,p,w,b,with_trace=True)
        f=copy.deepcopy(w.source);f[45]['mark']+=200;changed=Window(f,w.receipts);_,second,_=replay(s,p,changed,b,with_trace=True)
        self.assertEqual(first[:45],second[:45])
    def test_profile_binding_staleness_expiry_and_integer_wrap(self):
        p,s,w,b=fixture();wire=profile_wire(p);wire.position=8
        bad=copy.deepcopy(w.source);bad[1]['unix']=2**63
        with self.assertRaises(ValueError):Window(bad,w.receipts)
        with self.assertRaises(ValueError):Engine(s,p,wire=wire)
        with Engine(s,p,expires=100) as n:
            with self.assertRaises(ValueError):n.decide([0]*6,15000,now=100)
        with Engine(s,p) as n:
            with self.assertRaises(ValueError):n.decide([2**64]+[0]*5,15000)
            with self.assertRaises(ValueError):n.decide([0]*6,15000,now=3000000002,observed=1)
    def test_two_users_use_different_limits_without_mutation(self):
        a=profile('a','defensive');b=profile('b','active');s={'nodes':[[2,0,0,0,0],[2,0,0,0,6],[2,0,0,0,2]],'outputs':[0,1,2,0]}
        with Engine(s,a) as x,Engine(s,b) as y:
            a['limits']['position']=8
            dx=x.decide([4,0,0,0,0,0],15000);dy=y.decide([4,0,0,0,0,0],15000)
            self.assertTrue(dx['reduce']);self.assertTrue(all(q['side']==1 for q in dx['quotes']))
            self.assertLessEqual(max(q['lots'] for q in dx['quotes']),1);self.assertFalse(dy['reduce'])
    def test_real_sbf_downstream(self):
        sys.path.insert(0,str(SOURCE/'tests'));from test_sbf import Lab,quotes,ioc
        l=Lab(actors=2,collateral=10000000);p=profile('sbf','balanced')
        with Engine(candidates()[-1],p) as e:d=e.decide([0]*6,15000)
        tx=l.call(0,5,quotes([(q['side']*8,q['price'],q['lots'],l.slot+100,q['side'],q['reduce']) for q in d['quotes']]))
        ask=next(q['price'] for q in d['quotes'] if q['side']==1);fill=l.call(1,6,ioc(0,1,ask,minimum=1))
        st=l.check();self.assertEqual(st['seats'][0]['position'],-1);self.assertEqual(sum(s['position'] for s in st['seats']),0)
        (OUT/'sbf.json').write_text(json.dumps({'quote_cu':tx.compute_units_consumed(),'fill_cu':fill.compute_units_consumed(),'position_sum':0,'source':'actual wide256 SBF in LiteSVM; no devnet submission'}))

class RegistryTests(unittest.TestCase):
    def test_worker_gateway_recovers_archive_to_native_commit_gap(self):
        sys.path.insert(0,str(SOURCE));from research import gateway
        from archive import Archive
        p,s,w,b=fixture();frames=copy.deepcopy(w.source)
        for f in frames:f['id']+=1000
        with tempfile.TemporaryDirectory() as folder,patch.object(gateway,'STORE',Path(folder)/'native.sqlite'):
            a=Archive(Path(folder)/'budget.sqlite');gateway.register_profiles([p]);a.initialize(p)
            with patch('archive.time.time',return_value=994):attempt=a.reserve(p['profile_hash'],digest(s),990)
            a.result(attempt,'PROPOSED',{'finished_unix':995,'holdout_after_id':990})
            report_value=gateway.compare(s,s,p,frames,w.receipts,cutoff=990,finished_unix=995)
            a.commit(attempt,s,{'parent_mode':'champion','focus':['inventory'],'lesson':'test'},report_value)
            # Simulate process loss after budget/history commit but before C++ journal.
            with gateway.registry() as r:wire=r.get(p['user_namespace']);self.assertEqual(r.cursor(wire),0)
            gateway.reconcile_archive(a);gateway.reconcile_archive(a)
            with gateway.registry() as r:self.assertEqual(r.cursor(wire),1060)
            with self.assertRaises(ValueError):gateway.compare(s,s,p,frames,w.receipts,cutoff=990,finished_unix=995)
            updated=personalize(p,{'limits':{'position':3}});gateway.register_profiles([updated])
            with self.assertRaises(ValueError):gateway.InstalledEngine(s,p)
            with gateway.InstalledEngine(s,updated) as installed:self.assertEqual(installed.wire.revision,2)
            self.assertEqual(a.summary()['model_calls_reserved'],1);a.db.close()
    def test_versions_and_user_isolation_survive_reopen(self):
        p,s,w,b=fixture();q=profile('another','balanced')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.db'
            with Registry(path) as r:
                a=r.ensure(p);other=r.ensure(q);updated=personalize(p,{'limits':{'position':3}});v2=r.ensure(updated)
                self.assertEqual(a.revision,1);self.assertEqual(v2.revision,2);self.assertEqual(v2.parent.hex(),a.hash.hex());self.assertEqual(r.ensure(updated).revision,2)
                self.assertEqual(r.get(q['user_namespace']).revision,1)
                with self.assertRaises(ValueError):r.append(updated,1)
            with Registry(path) as r:
                with self.assertRaises(ValueError):r.get(p['user_namespace'],2**64)
                self.assertEqual(r.get(p['user_namespace'],1).position,6);self.assertEqual(r.get(p['user_namespace'],2).position,3)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_concurrent_compare_and_swap_only_one_writer_wins(self):
        p,s,w,b=fixture()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.db'
            with Registry(path) as r:r.ensure(p)
            def write(position):
                with Registry(path) as r:
                    try:r.append(personalize(p,{'limits':{'position':position}}),1);return True
                    except ValueError:return False
            with ThreadPoolExecutor(2) as pool:self.assertEqual(sum(pool.map(write,(2,3))),1)
    def test_outcome_idempotency_holdout_reuse_revision_and_user_binding(self):
        p,s,w,b=fixture()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.db'
            with Registry(path) as r:
                wire=r.ensure(p);metrics,_=compare(s,s,p,w,b,wire);out=r.commit(wire,s,s,w,b,metrics)
                r.commit(wire,s,s,w,b,metrics);self.assertEqual(r.cursor(wire),60)
                self.assertEqual(bytes(out),bytes(r.outcome(wire,60)))
                altered=Outcome.from_buffer_copy(bytes(out));altered.candidate.value[0]^=1
                self.assertNotEqual(r.lib.rp_outcome_append(r.ptr,C.byref(altered)),0)
                altered=Outcome.from_buffer_copy(bytes(out));altered.last_id=61
                self.assertNotEqual(r.lib.rp_outcome_append(r.ptr,C.byref(altered)),0)
                other=r.ensure(profile('another','balanced'));self.assertEqual(r.cursor(other),0)
                updated=r.ensure(personalize(p,{'weights':{'drawdown':70}}));self.assertEqual(r.cursor(updated),0)
                with self.assertRaises(ValueError):r.outcome(updated,60)
            with Registry(path) as r:self.assertEqual(r.cursor(wire),60);self.assertEqual(bytes(out),bytes(r.outcome(wire,60)))
    def test_corrupt_profile_and_outcome_fail_closed(self):
        for table in ('profiles','outcomes'):
            p,s,w,b=fixture()
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'state.db'
                with Registry(path) as r:
                    wire=r.ensure(p);result,_=compare(s,s,p,w,b,wire);r.commit(wire,s,s,w,b,result)
                db=sqlite3.connect(path);db.execute('UPDATE '+table+' SET checksum=zeroblob(32)');db.commit();db.close()
                with Registry(path) as r:
                    with self.assertRaises(ValueError):r.get(p['user_namespace']) if table=='profiles' else r.cursor(wire)
    def test_forged_promotion_and_profile_scope_rejected(self):
        p,s,w,b=fixture()
        with tempfile.TemporaryDirectory() as folder,Registry(Path(folder)/'state.db') as r:
            wire=r.ensure(p);metrics,_=compare(s,s,p,w,b,wire);metrics.eligible=1
            with self.assertRaises(ValueError):r.commit(wire,s,s,w,b,metrics)
            wrong=profile_wire(p,2,wire.hash.hex());wrong.position=100
            self.assertNotEqual(r.lib.rp_profile_append(r.ptr,C.byref(wrong),1),0)

if __name__=='__main__':
    out=OUT
    with (out/'tests.txt').open('w') as f:r=unittest.TextTestRunner(stream=f,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    result={'success':r.wasSuccessful(),'tests':r.testsRun,'errors':len(r.errors),'failures':len(r.failures),'lib_sha256':hashlib.sha256(LIB.read_bytes()).hexdigest()}
    (out/'tests.json').write_text(json.dumps(result,indent=2));print(json.dumps(result));sys.exit(not r.wasSuccessful())
