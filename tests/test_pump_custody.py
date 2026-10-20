import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research.wallet_map import Graph,select_jobs,MODEL
from economic_machine.pump_research.store import canonical,encode


def w(n):return encode(bytes([n])*32)


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);(self.base/'evidence/wallet-map-v1').mkdir(parents=True)
        self.labels([(w(10),'A'),(w(11),'A'),(w(12),'B')])
        self.g=Graph(self.base);self.addCleanup(lambda:self.g.close())
        with self.g.db:self.g.db.execute('INSERT INTO roots VALUES(?,?)',(w(99),canonical(dict(mint=w(99)))))

    def labels(self,rows):
        (self.base/'evidence/wallet-map-v1/custody-labels.json').write_text(canonical(dict(
            schema='economic-machine.custody-label-claims.v1',labels=[dict(chain='solana',address=a,entity=e,
                source_url='https://example.org/fixture',source_sha256='a'*64,evidence_type='third_party_label_claim') for a,e in rows])))

    def transfer(self,src,dst,slot=100,**body):
        with self.g.db:
            return self.g.edge(w(99),src,dst,'indexed_transfer',slot,slot*1000,
                signature='fixture'+str(slot),ix=0,tx_order=1,amount='1000000',symbol='SOL',**body)

    def case(self):return self.g.custody.snapshot()['cases'][0]

    def job(self,wallet,case):
        return self.g.db.execute('''SELECT f.id FROM frontier f JOIN custody_jobs j ON j.job=f.id
            WHERE f.wallet=? AND f.source='indexed_forward' AND j.case_id=?''',(wallet,case)).fetchone()[0]

    def test_exchange_a_to_b_to_solana_and_later_buy_stays_unattributed(self):
        entry=self.transfer(w(1),w(10));a=self.case()['id']
        self.assertTrue(self.g.custody.enter(self.job(w(11),a)))
        second=self.transfer(w(11),w(12),200)
        self.g.custody.leave()
        b=next(c for c in self.g.custody.snapshot()['cases'] if c['entity_claim']=='B')
        self.assertEqual(b['parent_case_ids'],[a])
        self.g.custody.enter(self.job(w(12),b['id']))
        exit_edge=self.transfer(w(12),w(2),300)
        with self.g.db:j=self.g.enqueue(w(99),w(2),300,300000,2)
        self.g.custody.leave();self.g.custody.enter(j)
        with self.g.db:buy=self.g.edge(w(99),w(2),w(98),'later_buy',301,301000,signature='buy')
        self.g.custody.leave()
        notes=self.g.custody.annotations()
        self.assertIn(buy,notes);self.assertIn(exit_edge,notes);self.assertIn(second,notes)
        self.assertFalse(any(self.g.custody.snapshot()['scope'][k] for k in
            ['same_funds_continuity_proven','cross_chain_reentry_verified','complete_to_today']))
        self.assertTrue(all(c['status']=='OPEN_CUSTODY_GAP' for c in self.g.custody.snapshot()['cases']))
        # No invented transfer links exchange A's separate deposit/hot wallets.
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM edges WHERE source=? AND target=?',(w(10),w(11))).fetchone()[0],0)

    def test_unknown_label_is_not_named_as_exchange(self):
        self.transfer(w(1),w(5),destination_labeled=True)
        c=self.case();self.assertIsNone(c['entity_claim'])
        self.assertEqual(c['classification'],'unresolved_labeled_account')
        self.assertEqual({r[0] for r in self.g.db.execute('SELECT wallet FROM frontier')},{w(5)})

    def test_conflicting_labels_do_not_merge_exchange_wallets(self):
        self.labels([(w(10),'A'),(w(10),'B'),(w(11),'A')]);self.g.custody.reload()
        self.transfer(w(1),w(10))
        self.assertIsNone(self.case()['entity_claim'])
        self.assertEqual({r[0] for r in self.g.db.execute('SELECT wallet FROM frontier')},{w(10)})

    def test_same_amount_and_time_never_resolves_custody(self):
        self.transfer(w(1),w(10));a=self.case()['id'];self.g.custody.enter(self.job(w(11),a))
        self.transfer(w(11),w(2),101);self.g.custody.leave()
        c=self.case();self.assertFalse(c['same_funds_continuity_proven'])
        self.assertFalse(c['customer_credit_verified']);self.assertEqual(c['status'],'OPEN_CUSTODY_GAP')

    def test_exchange_sweeps_do_not_multiply_same_entity_cases(self):
        self.transfer(w(1),w(10));a=self.case()['id'];self.g.custody.enter(self.job(w(10),a))
        self.transfer(w(10),w(11),101);self.g.custody.leave()
        self.assertEqual(self.g.custody.snapshot()['summary']['cases'],1)

    def test_dead_entry_revokes_candidate_dispatch_and_dead_outflow_is_hidden(self):
        entry=self.transfer(w(1),w(10));a=self.case()['id'];j=self.job(w(11),a)
        self.g.custody.enter(j);out=self.transfer(w(11),w(2),101);self.g.custody.leave()
        with self.g.db:
            self.g.db.execute("UPDATE edges SET body=json_set(body,'$.dead_branch',1) WHERE id IN (?,?)",(entry,out))
        self.assertFalse(self.g.custody.enter(j));self.assertEqual(self.case()['followup_examples'],[])
        self.assertEqual(self.g.custody.snapshot()['summary']['dead_entries'],1)

    def test_restart_keeps_cases_cursors_and_only_scans_new_edges(self):
        edge=self.transfer(w(1),w(10));self.g.custody.backfill();case=self.case()['id'];j=self.job(w(11),case)
        with self.g.db:self.g.db.execute("UPDATE frontier SET cursor='123' WHERE id=?",(j,))
        before=self.g.state('custody_scan_row');self.g.close();self.g=Graph(self.base)
        self.assertEqual(self.case()['id'],case);self.assertEqual(self.g.state('custody_scan_row'),before)
        self.assertEqual(self.g.custody.backfill(),0)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(j,)).fetchone()[0],'123')

    def test_new_labels_backfill_prior_edge_without_mutating_original_evidence(self):
        edge=self.transfer(w(1),w(5));body=self.g.db.execute('SELECT body FROM edges WHERE id=?',(edge,)).fetchone()[0]
        self.g.custody.backfill();self.labels([(w(5),'C')]);self.g.custody.backfill()
        self.assertEqual(self.case()['entity_claim'],'C')
        self.assertEqual(self.g.db.execute('SELECT body FROM edges WHERE id=?',(edge,)).fetchone()[0],body)

    def test_other_chain_label_rejected_not_joined_by_same_text(self):
        p=self.g.out/'custody-labels.json';d=json.loads(p.read_text());d['labels'][0]['chain']='ethereum';p.write_text(canonical(d))
        with self.assertRaisesRegex(ValueError,'CUSTODY_LABEL_ROW'):self.g.custody.reload()

    def test_source_label_does_not_discard_core_recipient(self):
        core=self.base/'evidence/core-trace-v1';core.mkdir()
        db=sqlite3.connect(core/'arkm_funding.sqlite3')
        db.execute('CREATE TABLE projection(source BLOB,destination BLOB,stamp INTEGER,slot INTEGER,signature TEXT,amount TEXT,symbol TEXT,destination_labeled INTEGER)')
        db.execute('INSERT INTO projection VALUES(?,?,?,?,?,?,?,?)',(bytes([1])*32,bytes([5])*32,1706000100,100,'fixture','1','SOL',1));db.commit();db.close()
        db=sqlite3.connect(core/'token_metadata.sqlite3');db.execute('CREATE TABLE projection(mint BLOB,creator BLOB,first_seen INTEGER)');db.close()
        with self.g.db:j=self.g.enqueue(w(99),w(1),None,1706000000000,0,'core_forward')
        class Scope:
            def guard(self):pass
        self.g.core_forward(dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?',(j,)).fetchone()),Scope())
        self.assertTrue(self.g.db.execute('SELECT 1 FROM frontier WHERE wallet=?',(w(5),)).fetchone())
        self.assertIsNone(self.case()['entity_claim'])

    def test_view_marks_custody_gap_without_mutating_graph_body(self):
        with self.g.db:
            self.g.node(w(1),'account','participant');edge=self.transfer(w(1),w(10))
        self.assertFalse(any(x['id']==edge for x in self.g.view()['edges']))
        # Display-only fixture: raw commitment itself is covered by commit_graph tests.
        from unittest.mock import patch
        def assessed(graph,ids,**kwargs):
            return {ident:dict(status='committed',assessment_current=True) for ident in ids}
        with patch('economic_machine.pump_research.wallet_map.latest_assessments',side_effect=assessed):
            e=next(x for x in self.g.view()['edges'] if x['id']==edge)
        self.assertTrue(e['evidence']['custody_gaps']);self.assertFalse(e['evidence']['path_funds_continuity_proven'])
        self.assertNotIn('custody_gaps',json.loads(self.g.db.execute('SELECT body FROM edges WHERE id=?',(edge,)).fetchone()[0]))

    def test_same_signed_transfer_from_two_parsers_is_one_case(self):
        self.transfer(w(1),w(10))
        with self.g.db:self.g.edge(w(99),w(1),w(10),'sol_transfer',100,None,signature='fixture100',ix=0)
        self.assertEqual(self.g.custody.snapshot()['summary']['cases'],1)

    def test_token_boundary_owner_is_a_hint_and_changed_owner_is_not_joined(self):
        self.transfer(w(1),w(3),destination_owner_evidence=dict(status='changed_owner',boundary_owner=w(10)))
        self.assertEqual(self.g.custody.snapshot()['summary']['cases'],0)
        self.transfer(w(1),w(4),101,destination_owner_evidence=dict(status='same_owner_at_both_boundaries',boundary_owner=w(10)))
        c=self.case();self.assertEqual(c['label_match_basis'],'token_boundary_owner_hint')
        self.assertEqual(c['account'],w(4));self.assertFalse(c['instruction_time_owner_proven'])

    def test_dead_ancestor_revokes_nested_exchange_candidate(self):
        entry=self.transfer(w(1),w(10));a=self.case()['id'];self.g.custody.enter(self.job(w(11),a))
        self.transfer(w(11),w(12),200);self.g.custody.leave()
        b=next(c for c in self.g.custody.snapshot()['cases'] if c['entity_claim']=='B')
        with self.g.db:self.g.db.execute("UPDATE edges SET body=json_set(body,'$.dead_branch',1) WHERE id=?",(entry,))
        self.assertFalse(self.g.custody.enter(self.job(w(12),b['id'])))

    def test_engine_prevents_custody_starvation_without_rewriting_model_choice(self):
        (self.g.out/'address-index.sqlite3').touch()
        with self.g.db:normal=self.g.enqueue(w(99),w(1),50,50000,0)
        self.transfer(w(1),w(10))
        class Budget:
            def reserve(self,req,now):return 1
            def finish(self,*args):pass
        response=canonical(dict(model=MODEL,choices=[dict(message=dict(content=canonical(dict(
            job_ids=[normal],reason='fixture intentionally ignores custody',remaining_gaps=[]))))]))
        with patch('economic_machine.pump_research.wallet_map.provider_call',return_value=response):
            scheduled=select_jobs(self.g,Budget(),None,None)
        record=json.loads(self.g.db.execute('SELECT body FROM investigations').fetchone()[0])
        self.assertEqual(record['job_ids'],[normal]);self.assertIsNotNone(record['engine_reserved_custody_job'])
        self.assertEqual([j['id'] for j in scheduled],record['scheduled_job_ids'])
        self.assertLessEqual(len(scheduled),8)


if __name__=='__main__':unittest.main()
