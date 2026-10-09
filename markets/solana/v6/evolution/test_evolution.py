import copy,json,os,random,sqlite3,sys,tempfile,time,unittest
from pathlib import Path
from schema import *
from runtime import Engine
from archive import Archive
from evaluate import features,manifest,replay,compare
from muse import request,parse,MODEL
from admission import assess
from preferences import personalize

def synthetic():
    frames=[];receipts=[]
    for i in range(60):
        t=1000+i*3;mark=15000+(i%10-5)
        frames.append({'id':i+1,'slot':100+i*8,'unix':t,'mark':mark,'oracle_slot':100+i*8,'publish_time':t,'funding':i//25,
            'imbalance':(i%5-2)*20,'momentum':i%7-3,'depth':2,'orders':[],
            'positions':[0]*12,'equities':[10000000]*12,'accounting_verified':True,'provenance':'controlled_devnet_self_play','external_holdout':False})
        side=i%2;receipts.append({'signature':str(i),'slot':100+i*8,'side':side,'lots':8,'turnover':8*(mark+24 if side==0 else mark-24)})
    return frames,receipts

class SchemaTests(unittest.TestCase):
    def setUp(self):self.p=profile('a','balanced');self.s=seed(self.p)
    def test_seed(self):validate_program(self.s);validate_profile(self.p)
    def test_rejects_source_and_extra_fields(self):
        for value in ({'code':'import os'},dict(self.s,exec='x')):
            with self.assertRaises(ValueError):validate_program(value)
    def test_rejects_forward_reference(self):
        s=copy.deepcopy(self.s);s['nodes'][2][1]=3
        with self.assertRaises(ValueError):validate_program(s)
    def test_rejects_overflow_interval(self):
        s={'nodes':[[1,0,0,0,4],[2,0,0,0,10000],[5,0,1,0,0]],'outputs':[2]*4}
        with self.assertRaises(ValueError):validate_program(s)
    def test_rejects_bool_as_int_and_type_confusion(self):
        s=copy.deepcopy(self.s);s['nodes'][0][4]=True
        with self.assertRaises(ValueError):validate_program(s)
        s=copy.deepcopy(self.s);s['outputs'][0]=6
        with self.assertRaises(ValueError):validate_program(s)
    def test_owner_tampering(self):
        p=copy.deepcopy(self.p);p['limits']['position']=100
        with self.assertRaises(ValueError):validate_profile(p)
        p['profile_hash']=digest({k:v for k,v in p.items() if k!='profile_hash'})
        with self.assertRaises(ValueError):validate_profile(p)
    def test_custom_preference_revision(self):
        p=personalize(self.p,{'weights':{'drawdown':250},'limits':{'position':3}})
        self.assertNotEqual(p['profile_hash'],self.p['profile_hash']);self.assertEqual(self.p['limits']['position'],6)
        with self.assertRaises(ValueError):personalize(self.p,{'live_promotion':True})
        with self.assertRaises(ValueError):personalize(self.p,{'limits':{'clip':100}})
    def test_structure_detects_logic_change_not_constant(self):
        s=copy.deepcopy(self.s);s['nodes'][3][4]+=1;self.assertEqual(structure(s),structure(self.s));s['outputs'][0]=0;self.assertNotEqual(structure(s),structure(self.s))
    def test_request_projection(self):
        req=canonical(request(self.s,self.p,[],{'parent_mode':'champion','focus':['inventory'],'lesson':'x'}))
        self.assertNotIn('user_namespace',req);self.assertNotIn(self.p['profile_hash'],req);self.assertNotIn('actor_seat',req)
    def test_provider_and_duplicate_keys(self):
        value={'model':MODEL,'provider':'Meta','choices':[{'finish_reason':'stop','message':{'content':canonical({'program':self.s,'meta':{'parent_mode':'champion','focus':['inventory'],'lesson':'x'}})}}]}
        parse(value);value['model']='other'
        with self.assertRaises(ValueError):parse(value)

class RuntimeTests(unittest.TestCase):
    def test_all_numeric_and_boolean_ops_parity(self):
        p=profile('a','active');r=random.Random(77)
        for _ in range(40):
            nodes=[[1,0,0,0,0],[1,0,0,0,2],[2,0,0,0,2]]
            for op in (3,4,5,6,7,8,9,10,11):nodes.append([op,0,1 if op not in (8,9) else 0,0,0])
            nodes.extend([[12,10,11,0,0],[13,10,11,0,0],[14,12,0,0,0],[15,14,0,1,0]])
            s={'nodes':nodes,'outputs':[r.choice([0,1,3,4,5,6,7,8,9,15]),2,2,2]};validate_program(s)
            with Engine(s,p) as e:
                for i in range(20):
                    f=[r.randint(*bound) for bound in BOUNDS];self.assertEqual(e.raw(f,i+2)[0],evaluate_python(s,f))
    def test_native_reference_parity(self):
        p=profile('a','balanced');s=seed(p);r=random.Random(90210)
        with Engine(s,p) as e:
            for i in range(1000):
                f=[r.randint(*bound) for bound in BOUNDS];self.assertEqual(e.raw(f,now=i+2)[0],evaluate_python(s,f))
    def test_stale_invalid_expired(self):
        p=profile('a','balanced')
        for f,n,o,exp in (([9,0,0,0,0,0],2,2,100),([0]*6,3000000002,1,10**18),([0]*6,101,101,100)):
            with Engine(seed(p),p,expires=exp) as e:
                with self.assertRaises(ValueError):e.raw(f,n,o)
    def test_risk_and_profile_copy(self):
        p=profile('a','defensive')
        with Engine(seed(p),p) as e:
            p['limits']['position']=100
            for pos in range(-8,9):
                d=e.decide([pos,0,0,0,0,0],15000)
                for q in d['quotes']:
                    if abs(pos)>=4:self.assertTrue(q['reduce']);self.assertTrue((pos>0 and q['side']==1) or (pos<0 and q['side']==0))
                    self.assertLessEqual(q['lots'],1)
    def test_post_only_and_bad_price(self):
        p=profile('a','active')
        with Engine(seed(p),p) as e:
            d=e.decide([0]*6,15000,15000,15001)
            for q in d['quotes']:self.assertTrue(q['price']<15001 if q['side']==0 else q['price']>15000)
            with self.assertRaises(ValueError):e.decide([0]*6,-1)

class EvaluationTests(unittest.TestCase):
    def test_temporal_and_provenance(self):
        f,r=synthetic();manifest(f,r)
        for mutate in ('order','stale','provenance'):
            g=copy.deepcopy(f)
            if mutate=='order':g[2]['slot']=g[1]['slot']
            if mutate=='stale':g[2]['publish_time']-=100
            if mutate=='provenance':g[2]['accounting_verified']=False
            with self.assertRaises(ValueError):manifest(g,r)
    def test_no_future_feature_leak(self):
        f,r=synthetic();a=features(f[:20],0);f[30]['mark']=10000000;self.assertEqual(a,features(f[:20],0))
    def test_same_strategy_never_promoted(self):
        f,r=synthetic();p=profile('a','balanced');s=seed(p);out=compare(s,s,p,f,r)
        self.assertFalse(out['research_champion_eligible']);self.assertFalse(out['live_promotion_eligible']);self.assertEqual(out['scenarios'][0]['score_delta'],0)
    def test_fill_pnl_and_risk_are_deterministic(self):
        f,r=synthetic();p=profile('a','defensive');s=seed(p);a=replay(s,p,f,r);b=replay(s,p,f,r)
        self.assertEqual(a['trace_hash'],b['trace_hash']);self.assertLessEqual(a['max_position'],4);self.assertGreater(a['filled_lots_estimate'],0);self.assertGreaterEqual(a['exit_fee_ticks'],0)
    def test_inactivity_is_not_an_improvement(self):
        f,r=synthetic();p=profile('a','balanced');s=seed(p);c=copy.deepcopy(s);c['outputs'][3]=5
        self.assertFalse(compare(s,c,p,f,r)['research_champion_eligible'])
    def test_promotion_blocks_synthetic_and_unreconciled(self):
        p=profile('a','balanced');s=seed(p);f,r=synthetic();report=compare(s,s,p,f,r)
        report.update(program_hash=digest(s),profile_hash=p['profile_hash'])
        result=assess(s,p,report,{'position':1,'pending_signatures':1},2000)
        self.assertFalse(result['eligible']);self.assertIn('no independent holdout',result['reasons']);self.assertFalse(result['execution_authority'])

class ArchiveTests(unittest.TestCase):
    def test_budget_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.db';a=Archive(path);p=profile('a','balanced');a.initialize(p);h=p['profile_hash']
            for i in range(12):a.reserve(h,a.profile(h)['champion'],i+1)
            a.db.close();a=Archive(path)
            with self.assertRaises(ValueError):a.reserve(h,a.profile(h)['champion'],20)
            self.assertLessEqual(a.summary()['reserved_cost_ceiling_usd'],0.03)
    def test_profile_isolation_and_holdout_reuse(self):
        with tempfile.TemporaryDirectory() as d:
            a=Archive(Path(d)/'a.db');p=profile('a','balanced');q=profile('b','balanced');a.initialize(p);a.initialize(q)
            h=p['profile_hash'];parent=a.profile(h)['champion'];i=a.reserve(h,parent,100)
            record={'finished_unix':time.time(),'holdout_after_id':101};a.result(i,'PROPOSED',record)
            report={'window':{'first_id':102,'last_id':150,'first_unix':time.time()+1},'research_champion_eligible':False}
            meta={'parent_mode':'recent','focus':['inventory'],'lesson':'x'};a.commit(i,seed(p),meta,report)
            self.assertEqual(a.profile(h)['cursor'],150);self.assertEqual(a.profile(q['profile_hash'])['cursor'],0)
            with self.assertRaises(ValueError):a.reserve(h,parent,149)
    def test_preproposal_holdout_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            a=Archive(Path(d)/'a.db');p=profile('a','balanced');a.initialize(p);h=p['profile_hash'];i=a.reserve(h,a.profile(h)['champion'],100)
            a.result(i,'PROPOSED',{'finished_unix':time.time()+20,'holdout_after_id':110})
            with self.assertRaises(ValueError):a.commit(i,seed(p),{'parent_mode':'recent','focus':['inventory'],'lesson':'x'},{'window':{'first_id':105,'first_unix':time.time()+1},'research_champion_eligible':False})

class SbfTests(unittest.TestCase):
    def test_new_graph_executes_real_program_and_reconciles(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
        from test_sbf import Lab,quotes,ioc
        l=Lab(actors=2,collateral=10000000);p=profile('a','balanced');s=seed(p)
        # A different graph wiring (momentum-driven center), not a parameter edit.
        s['nodes'].append([1,0,0,0,2]);s['outputs'][0]=10
        with Engine(s,p) as e:d=e.decide([0,0,3,0,0,0],15000)
        entries=[(q['side']*8,q['price'],q['lots'],l.slot+100,q['side'],q['reduce']) for q in d['quotes']]
        qtx=l.call(0,5,quotes(entries),name='evolved-graph-quotes')
        ask=next(q['price'] for q in d['quotes'] if q['side']==1)
        itx=l.call(1,6,ioc(0,1,ask,minimum=1),name='evolved-graph-fill')
        st=l.check();self.assertEqual(st['seats'][0]['position'],-1);self.assertEqual(sum(s['position'] for s in st['seats']),0)
        (Path(__file__).resolve().parents[1]/'evidence/sbf-evolution.json').write_text(json.dumps({'quote_cu':qtx.compute_units_consumed(),'ioc_cu':itx.compute_units_consumed(),'positions':[s['position'] for s in st['seats']],'conserved':True,'source':'real v6 SBF in LiteSVM, not devnet submission'}))

if __name__=='__main__':unittest.main()
