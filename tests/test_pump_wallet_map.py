import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from economic_machine.pump_research.wallet_map import Graph,UncappedMuse,MODEL,select_jobs
from economic_machine.pump_research.store import encode,Store

def wallet(n):return encode(bytes([n])*32)

class ModelTests(unittest.TestCase):
    def test_legacy_create_without_creator_uses_explicit_payer_role(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'evidence').mkdir();s=Store(base/'data')
            with s.db:
                s.db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?)',(wallet(10),'coin','COIN',wallet(3),1706000000000,'1000','provider_claim_unverified',1,None))
                s.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)',('o','signature',100,1,'old-faithful','archive_content_verified',1,None))
                s.db.execute('INSERT INTO launches VALUES(?,?,?,?,?,?,?,?)',('o',0,wallet(10),wallet(2),'','coin','COIN',100))
            s.db.close();g=Graph(base)
            try:
                g.seed();self.assertEqual(g.db.execute("SELECT count(*) FROM nodes WHERE id='' ").fetchone()[0],0)
                e=g.db.execute("SELECT source,kind FROM edges WHERE kind='launch_payer'").fetchone()
                self.assertEqual(tuple(e),(wallet(2),'launch_payer'))
                self.assertEqual(g.db.execute("SELECT count(*) FROM frontier WHERE wallet='' ").fetchone()[0],0)
            finally:g.close()
    def test_empty_model_response_is_rejected_and_cost_accounted(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'evidence').mkdir();g=Graph(base)
            scope=dict(authorization='owner_unlimited_muse_top10_forward_wallet_research',maximum_usd=None,max_calls=None,
                model=MODEL,trade_authority=False,deadline_ms=time.time_ns()//1000000+100000)
            budget=UncappedMuse(base/'calls.sqlite3',scope)
            try:
                with g.db:
                    g.db.execute('INSERT INTO roots VALUES(?,?)',(wallet(9),'{}'));g.enqueue(wallet(9),wallet(1),100,1706000000000,0)
                raw=json.dumps(dict(model=MODEL,choices=[dict(message=dict(content=None),finish_reason='length')],usage=dict(cost=.0001)))
                with patch('economic_machine.pump_research.wallet_map.provider_call',return_value=raw) as call:
                    with self.assertRaisesRegex(ValueError,'EMPTY_RESPONSE'):select_jobs(g,budget,'unused',None)
                    self.assertEqual(call.call_args.args[0]['reasoning']['effort'],'minimal')
                self.assertEqual(budget.totals()['responses_received'],1);self.assertEqual(budget.totals()['accepted_responses'],0)
                self.assertEqual(budget.totals()['unconfirmed_cost_reservations'],0)
            finally:budget.db.close();g.close()
    def test_seed_ten_without_inventing_launches_or_buys(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'evidence').mkdir();s=Store(base/'data')
            with s.db:
                for i in range(10):
                    s.db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?)',(wallet(i+10),'coin','COIN',wallet(i+30),1706000000000,'1000','provider_claim_unverified',i+1,None))
            s.db.close();g=Graph(base)
            try:
                g.seed();g.seed();v=g.view()
                self.assertEqual(len(v['roots']),10);self.assertEqual(len(v['edges']),10)
                self.assertTrue(all(e['kind']=='provider_creator_claim' for e in v['edges']))
                self.assertEqual(g.db.execute("SELECT count(*) FROM frontier WHERE source='recover_launch'").fetchone()[0],10)
            finally:g.close()
    def test_no_call_or_total_cost_ceiling_with_durable_meter(self):
        with tempfile.TemporaryDirectory() as d:
            scope=dict(authorization='owner_unlimited_muse_top10_forward_wallet_research',maximum_usd=None,max_calls=None,
                model=MODEL,trade_authority=False,deadline_ms=time.time_ns()//1000000+100000)
            b=UncappedMuse(Path(d)/'calls.sqlite3',scope)
            try:
                for i in range(1101):
                    ident=b.reserve(dict(model=MODEL,max_tokens=4096,messages=[str(i)]),time.time_ns()//1000000)
                self.assertEqual(ident,1101);self.assertEqual(b.totals()['calls_reserved'],1101)
                self.assertGreater(float(b.totals()['cost_committed_or_reserved_usd']),.03)
                with self.assertRaises(ValueError):b.reserve(dict(model=MODEL,max_tokens=4096),scope['deadline_ms'])
            finally:b.db.close()
    def test_model_cannot_add_authority(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):UncappedMuse(Path(d)/'a',dict(maximum_usd=None,max_calls=None,model=MODEL,trade_authority=True))

class MapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        (self.base/'evidence').mkdir();(self.base/'data').mkdir()
        self.g=Graph(self.base);self.addCleanup(self.g.close)
        self.db=sqlite3.connect(self.base/'data/search.sqlite3');self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE transfers(observation TEXT,source TEXT,destination TEXT,mint TEXT,kind TEXT,amount TEXT,slot INTEGER,ix INTEGER); CREATE TABLE observations(id TEXT,signature TEXT,tx_order INTEGER,finality TEXT,evidence TEXT);')
    def transfer(self,num,tx_order,slot=100,finality='archive_content_verified'):
        self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?)',(str(num),'sig'+str(num),tx_order,finality,'evidence'))
        self.db.execute('INSERT INTO transfers VALUES(?,?,?,?,?,?,?,?)',(str(num),wallet(1),wallet(num+3),'SOL','system_transfer','100000000',slot,0));self.db.commit()
    def test_forward_has_causal_instruction_order_and_excludes_dead_branch(self):
        self.transfer(1,1);self.transfer(2,3);self.transfer(3,4,finality='dead_branch')
        with self.g.db:
            self.g.node(wallet(1),'account','observed_early_buyer')
            jid=self.g.enqueue(wallet(9),wallet(1),100,1706000000000,0,after_order=(2,0))
        row=dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?',(jid,)).fetchone())
        result=self.g.local_forward(row);self.assertEqual(result['observed_transfers'],1)
        edge=self.g.db.execute('SELECT body FROM edges').fetchone()[0];self.assertIn('sig2',edge)
        r=self.g.view();self.assertFalse(r['complete_to_today']);self.assertFalse(r['bundle_membership_verified']);self.assertEqual(r['orders_submitted'],0)
    def test_duplicate_edge_and_frontier_stable(self):
        with self.g.db:
            for _ in range(2):
                self.g.edge(wallet(1),wallet(2),wallet(3),'sol_transfer',100,None,signature='sig')
                self.g.enqueue(wallet(1),wallet(2),100,None,0,after_order=(1,0))
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM edges').fetchone()[0],1)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM frontier').fetchone()[0],1)

if __name__=='__main__':unittest.main()
