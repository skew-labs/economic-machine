from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import random
import tempfile
import unittest

from economic_machine.muse_learning import (BOUNDS, Graph, ResearchBudget, Store, baseline, compare, grammar, lower_boolean_values,
    proposal, research_request, validate_available)
from economic_machine.muse_maker import MODEL

T=1792000000000
LEASE={'session_id':'research-test','start_ms':T,'deadline_ms':T+172800000}
LIB=os.environ.get('MUSE_RESEARCH_LIB')
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('research_capture',ROOT/'products/pacifica_muse/research_capture.py')
capture=importlib.util.module_from_spec(spec);spec.loader.exec_module(capture)
spec=importlib.util.spec_from_file_location('research_cycle',ROOT/'products/pacifica_muse/research_cycle.py')
cycle=importlib.util.module_from_spec(spec);spec.loader.exec_module(cycle)


def fill():
    return {'history_id':1,'order_id':2,'client_order_id':'owned-cid','symbol':'SOL','amount':'1',
            'price':'100','fee':'-0.005','pnl':'0.005','event_type':'fulfill_maker','side':'open_long','created_at':T,'cause':'normal'}


def intent():return {'kind':'unsigned_intent','op':'create_order','data':{'symbol':'SOL','client_order_id':'owned-cid'},'at_ms':T}


def status(t=T):
    return {'session_id':LEASE['session_id'],'updated_ms':t,'public_book':{'bid':'99.99','ask':'100.01','source_ms':t},'net_SOL':0,'policy_version':1}


def response(program=None):
    body={'analysis':{'trend':'flat','volatility':'unknown','usable':['bbo'],
                      'missing':['flow','volatility','own_fill_markout','queue','pool_detail']},
          'program':program or baseline(),'focus':'flow'}
    return {'model':MODEL,'provider':'Meta','choices':[{'finish_reason':'stop','message':{'content':json.dumps(body)}}]}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'research',LEASE)
    def tearDown(self):self.store.close();self.tmp.cleanup()

    def test_optional_features_missing_does_not_discard_public_book(self):
        self.assertTrue(self.store.frame(status(),{},T))
        f=json.loads(self.store.db.execute('SELECT body FROM frames').fetchone()[0])
        self.assertFalse(f['flow_available']);self.assertFalse(f['volatility_available'])
        self.assertEqual(f['features'],[0,0,0,0,2,0])

    def test_actual_stale_book_and_runtime_are_rejected(self):
        self.assertFalse(self.store.frame(status(),{},T+2001))
        s=status(T+3000);s['updated_ms']=T
        self.assertFalse(self.store.frame(s,{},T+3000))

    def test_wrong_session_and_rebinding_rejected(self):
        s=status();s['session_id']='foreign'
        with self.assertRaises(ValueError):self.store.frame(s,{},T)
        with self.assertRaises(ValueError):Store(self.root/'research',dict(LEASE,session_id='foreign'))

    def test_invalid_optional_numbers_do_not_become_available(self):
        obs={'archive_failed':False,'model':{'at_wall_ms':T,'flow':float('nan'),'sigma_bps':-1,'onchain_available':True}}
        self.assertTrue(self.store.frame(status(),obs,T))
        f=json.loads(self.store.db.execute('SELECT body FROM frames').fetchone()[0])
        self.assertFalse(f['flow_available']);self.assertFalse(f['volatility_available'])

    def test_foreign_fill_excluded_then_attributed_by_journal(self):
        self.store._fill(fill());r=self.store.report()
        self.assertEqual((r['attributed_fills'],r['unattributed_fill_records']),(0,1))
        self.assertEqual(r['economics']['reported_trade_pnl_after_fee_usd'],'0')
        self.store.ingest(intent(),7);r=self.store.report()
        self.assertEqual((r['attributed_fills'],r['unattributed_fill_records']),(1,0))
        self.assertEqual(r['economics']['reported_trade_pnl_after_fee_usd'],'0.005')
        self.assertEqual(r['executed_policy_receipts']['7']['fills'],1)
        self.assertFalse(r['own_fill_learning_ready'])

    def test_fee_and_duplicate_receipt_count_once(self):
        self.store.ingest(intent(),3);self.store._fill(fill());self.store._fill(fill())
        r=self.store.report();self.assertEqual(r['attributed_fills'],1)
        self.assertEqual(r['economics']['reported_trade_pnl_after_fee_usd'],'0.005')
        changed=fill();changed['pnl']='1'
        with self.assertRaises(ValueError):self.store._fill(changed)

    def test_crash_after_attribution_repairs_derived_receipt(self):
        self.store.ingest(intent(),3)
        real=self.store.receipts.trade
        def crash(_):raise RuntimeError('simulated crash')
        self.store.receipts.trade=crash
        with self.assertRaises(RuntimeError):self.store._fill(fill())
        self.store.receipts.trade=real;self.store.close();self.store=Store(self.root/'research',LEASE)
        self.assertEqual(self.store.report()['economics']['fills'],1)

    def test_bad_fill_cannot_commit_attribution(self):
        self.store.ingest(intent(),3);bad=fill();bad['amount']='NaN'
        with self.assertRaises(ValueError):self.store._fill(bad)
        self.assertEqual(self.store.report()['attributed_fills'],0)

    def test_marks_missing_not_winning_and_first_future_mark_frozen(self):
        self.store.ingest(intent(),3);self.store._fill(fill())
        self.assertEqual(self.store.report()['economics']['markouts']['1000']['missing'],1)
        self.store.frame(status(T+1000),{},T+1010)
        s=status(T+1100);s['public_book'].update(bid='109.99',ask='110.01')
        self.store.frame(s,{},T+1110)
        r=self.store.report()['economics']['markouts']
        self.assertEqual(r['1000']['markout_after_fee_usd'],'0.005')
        self.assertEqual(r['100']['missing'],1)

    def test_input_paths_are_protected(self):
        for root in ('live','live/nested','.'):
            with self.assertRaises(ValueError):capture.separate(self.root/root,self.root/'live')

    def test_journal_resume_partial_line_and_replacement(self):
        path=self.root/'events.jsonl'
        policy={'kind':'policy_installed','version':3,'at_ms':T}
        path.write_text(json.dumps(policy)+'\n'+json.dumps(intent())[:20])
        offset=capture.journal(self.store,path)
        self.assertEqual(capture.journal(self.store,path),offset)
        with path.open('a') as f:f.write(json.dumps(intent())[20:]+'\n')
        capture.journal(self.store,path)
        self.store._fill(fill());self.assertEqual(self.store.report()['attributed_fills'],1)
        path.write_text('')
        with self.assertRaises(ValueError):capture.journal(self.store,path)

    def test_snapshot_readonly_export(self):
        self.store.frame(status(),{},T);capture.snapshot(self.store,self.store.report())
        import sqlite3
        db=sqlite3.connect((self.root/'research/snapshot.sqlite').as_uri()+'?mode=ro',uri=True)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM frames').fetchone()[0],1)
        with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM frames')
        db.close()


class ModelTests(unittest.TestCase):
    def test_future_evaluation_feedback_is_projected_into_next_request(self):
        feedback={'labels':30,'parent_mae_ticks':1.0,'candidate_mae_ticks':.5,'improved_forecast':True,'raw_account':'NEVER-SEND'}
        req=research_request(baseline(),[],feedback,{})
        body=json.loads(req['messages'][1]['content'])
        self.assertEqual(body['previous_future_evaluation']['candidate_mae_ticks'],.5)
        self.assertNotIn('NEVER-SEND',json.dumps(req))

    def test_next_research_parent_requires_completed_improving_evaluation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=baseline();p['nodes'][0][4]=1
            (root/'candidate-1.json').write_text(json.dumps({'generation':1,'proposal':{'program':p}}))
            with self.assertRaises(ValueError):cycle.previous(root)
            (root/'evaluation-1.json').write_text(json.dumps({'improved_forecast':False}))
            self.assertEqual(cycle.previous(root)[0],baseline())
            (root/'evaluation-1.json').write_text(json.dumps({'improved_forecast':True}))
            self.assertEqual(cycle.previous(root)[0],p)

    def test_research_output_allowance_fully_reserved(self):
        from decimal import Decimal
        from economic_machine.muse_policy_worker import Budget
        req=research_request(baseline(),[],{}, {})
        with tempfile.TemporaryDirectory() as d:
            b=ResearchBudget(Path(d)/'model.sqlite','research-test',T+10000,'0.02',1)
            b.reserve(req,T)
            self.assertGreaterEqual(Decimal(b.totals()['cost_committed_or_reserved_usd']),Decimal(4096)*Decimal('0.0000002'))
            with self.assertRaises(ValueError):b.reserve(req,T+1)
            b.db.close()
            live=Budget(Path(d)/'live.sqlite','live-test',T+10000,'0.02',1)
            with self.assertRaises(ValueError):live.reserve(req,T)
            live.db.close()

    def test_no_live_mode_and_no_optional_signal_invention(self):
        p=proposal(json.dumps(response()));frames=[{'flow_available':False,'volatility_available':False}]
        validate_available(p,frames,{})
        p['analysis']['usable'].append('flow')
        with self.assertRaises(ValueError):validate_available(p,frames,{})
        raw=response();body=json.loads(raw['choices'][0]['message']['content']);body['mode']='quote'
        raw['choices'][0]['message']['content']=json.dumps(body)
        with self.assertRaises(ValueError):proposal(json.dumps(raw))

    def test_wrong_provider_and_tool_call_rejected(self):
        raw=response();raw['provider']='Other'
        with self.assertRaises(ValueError):proposal(json.dumps(raw))
        raw=response();raw['choices'][0]['message']['tool_calls']=[{'function':'order'}]
        with self.assertRaises(ValueError):proposal(json.dumps(raw))

    def test_invalid_signal_type_rejected(self):
        raw=response();body=json.loads(raw['choices'][0]['message']['content']);body['analysis']['usable']=[{}]
        raw['choices'][0]['message']['content']=json.dumps(body)
        with self.assertRaises(ValueError):proposal(json.dumps(raw))

    def test_request_allowlist_and_zero_fills_honesty(self):
        f={'features':[0,0,0,0,2,0],'flow_available':False,'volatility_available':False,'private_key':'NEVER-SEND'}
        req=research_request(baseline(),[f],{}, {'attributed_fills':0,'secret':'NEVER-SEND'})
        self.assertNotIn('NEVER-SEND',json.dumps(req))
        self.assertEqual(req['model'],MODEL)
        body=json.loads(req['messages'][1]['content'])
        self.assertFalse(body['own_fill_evidence']['ready'])
        self.assertTrue(body['own_fill_evidence']['candidate_has_never_traded'])

    def test_cycles_overflow_boolean_output_rejected(self):
        programs=[{'nodes':[[3,0,0,0,0]],'outputs':[0]*4},
                  {'nodes':[[1,0,0,0,4],[5,0,0,0,0]],'outputs':[1]*4},
                  {'nodes':[[2,0,0,0,0],[10,0,0,0,0]],'outputs':[1]*4}]
        for p in programs:
            with self.subTest(p=p),self.assertRaises(ValueError):grammar().validate_program(p)

    def test_constants_only_do_not_count_as_structural_invention(self):
        p=baseline();q=deepcopy(p);q['nodes'][0][4]=1
        self.assertEqual(grammar().structure(p),grammar().structure(q))


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not LIB:raise RuntimeError('MUSE_RESEARCH_LIB must reference the remote-built library')

    def test_native_python_match_with_conditional_graph(self):
        p={'nodes':[[1,0,0,0,1],[2,0,0,0,0],[10,0,1,0,0],[2,0,0,0,2],
                    [2,0,0,0,-2],[15,2,3,4,0],[1,0,0,0,3],[8,0,0,0,0]],'outputs':[5,6,7,3]}
        rng=random.Random(98)
        with Graph(p,LIB) as g:
            for _ in range(500):
                f=[rng.randint(lo,hi) for lo,hi in BOUNDS]
                self.assertEqual(g.evaluate(f),grammar().evaluate_python(p,f))

    def test_bad_features_and_closed_graph(self):
        with Graph(baseline(),LIB) as g:
            for f in ([0]*5,[True,0,0,0,0,0],[9,0,0,0,0,0]):
                with self.assertRaises(ValueError):g.evaluate(f)
        with self.assertRaises(ValueError):g.evaluate([0]*6)

    def test_boolean_lowering_preserves_values_and_strict_native_types(self):
        p={'nodes':[[1,0,0,0,1],[2,0,0,0,2],[10,0,1,0,0],[15,2,1,2,0]],'outputs':[0,1,2,3]}
        with self.assertRaises(ValueError):grammar().validate_program(p)
        lowered,meta=lower_boolean_values(p)
        self.assertEqual(meta['cast_source_nodes'],[2])
        with Graph(lowered,LIB) as g:
            for flow in range(-100,101):
                out=g.evaluate([0,flow,0,0,0,0])
                self.assertEqual(out,[max(-24,min(24,flow)),2,int(flow>2),2 if flow>2 else 0])
        bad=deepcopy(p);bad['nodes'][3][1]=0
        with self.assertRaises(ValueError):lower_boolean_values(bad)

    def test_lowering_rejects_overflow_and_numeric_boolean_operations(self):
        bad={'nodes':[[1,0,0,0,4],[5,0,0,0,0]],'outputs':[1]*4}
        with self.assertRaises(ValueError):lower_boolean_values(bad)
        bad={'nodes':[[2,0,0,0,1],[12,0,0,0,0]],'outputs':[1]*4}
        with self.assertRaises(ValueError):lower_boolean_values(bad)

    def frames(self):
        return [{'id':i+1,'source_ms':T+3000+i*1000,'received_ms':T+3010+i*1000,
                 'mid':str(100+i/100),'features':[0,0,1,0,2,0]} for i in range(31)]

    def test_prospective_score_is_not_profitability(self):
        p=baseline();p['nodes'][0][4]=1
        r=compare(baseline(),p,self.frames(),completed_ms=T,cutoff_id=0,library=LIB)
        self.assertEqual(r['labels'],30);self.assertTrue(r['improved_forecast'])
        self.assertEqual(r['candidate_mae_ticks'],0)
        self.assertFalse(r['live_promotion_eligible']);self.assertFalse(r['fill_model_validated'])

    def test_before_completion_or_snapshot_cutoff_rejected(self):
        for cutoff,completed in ((1,T),(0,T+1000)):
            with self.assertRaises(ValueError):compare(baseline(),baseline(),self.frames(),completed_ms=completed,cutoff_id=cutoff,library=LIB)

    def test_missing_future_or_stale_labels_not_interpolated(self):
        frames=self.frames()[::2]
        r=compare(baseline(),baseline(),frames,completed_ms=T,cutoff_id=0,library=LIB)
        self.assertEqual(r['labels'],0);self.assertIsNone(r['candidate_mae_ticks'])
        frames=self.frames()
        for f in frames:f['received_ms']=f['source_ms']+2001
        r=compare(baseline(),baseline(),frames,completed_ms=T,cutoff_id=0,library=LIB)
        self.assertEqual(r['labels'],0)


if __name__=='__main__':unittest.main()
