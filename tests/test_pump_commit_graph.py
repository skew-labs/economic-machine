from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from economic_machine.pump_research.address_index import AddressIndex,install_commit_revision_tracking
from economic_machine.pump_research.commit_graph import assess_edges,assess_new_edges,latest_assessments,edge_requires_commit,assessment_snapshot
from economic_machine.pump_research.store import canonical,encode


def key(n):return encode(bytes([n])*32)
def sig(n):return encode(bytes([n])*64)


class CommitGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.index=AddressIndex(Path(self.tmp.name)/'address-index.sqlite3');self.addCleanup(self.index.close)
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;self.addCleanup(db.close)
        db.executescript('''CREATE TABLE edges(id TEXT PRIMARY KEY,root TEXT,source TEXT,target TEXT,kind TEXT,slot INTEGER,stamp INTEGER,body TEXT);
          CREATE TABLE frontier(id TEXT PRIMARY KEY,cursor TEXT); INSERT INTO frontier VALUES('original','123');''')
        self.graph=NS(db=db);self.scope=NS(guard=lambda:None);self.n=0
    def tx(self,ix=0,sha='a'*64,success=True,policy=None,source=None,signed=True,n=1):
        tx=dict(signature=sig(n),slot=250000000,index=0,success=success,account_keys=[key(1),key(2),key(3)],
            transfers=[dict(ix=ix,source=key(1),destination=key(2),mint='SOL',amount='100',kind='system_transfer')],
            activity=[dict(ix=ix,wallet=key(1),mint=key(3),action='buy')],launches=[])
        if policy is not None:tx['instruction_commit_evidence']=dict(version=1,committed_inner_orders=policy)
        block=dict(slot=250000000,time=1706000000,transactions=[tx],transaction_nodes=1,decode_errors=[])
        receipt=dict(car_sha256=sha,transaction_signatures_verified=signed)
        if source:receipt['source']=source
        self.index.ingest(block,receipt);return block
    def edge(self,kind='indexed_transfer',ix=0,sha='a'*64,n=1,**changes):
        self.n+=1;ident=f'edge-{self.n}'
        body=dict(signature=sig(n),ix=ix,amount='100',symbol='SOL')
        if sha is not None:body['source_sha256']=sha
        body.update(changes)
        target=key(3) if kind=='later_buy' else key(2)
        with self.graph.db:self.graph.db.execute('INSERT INTO edges VALUES(?,?,?,?,?,?,?,?)',
            (ident,key(9),key(1),target,kind,250000000,1706000000000,canonical(body)))
        return ident
    def status(self,ident):return latest_assessments(self.graph,[ident])[ident]['status']
    def test_top_level_requires_matching_successful_raw_transaction(self):
        self.tx();ident=self.edge();assess_edges(self.graph,self.index,self.scope)
        self.assertEqual(self.status(ident),'committed')
        value=latest_assessments(self.graph,[ident])[ident]
        self.assertFalse(value['independent_consensus_verified']);self.assertFalse(value['same_funds_continuity_proven'])
    def test_unresolved_old_source_is_not_top_level_commit_proof(self):
        ident=self.edge(sha=None);assess_edges(self.graph,self.index,self.scope)
        self.assertEqual(self.status(ident),'legacy_unchecked')
    def test_processed_status_without_signature_proof_is_unchecked(self):
        self.tx(source='ticking_retained',signed=False);ident=self.edge(finality='filtered_processed_stream')
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'legacy_unchecked')
    def test_cached_raw_audit_proves_only_the_transactions_it_reverified(self):
        self.tx(signed=False);ident=self.edge()
        with self.index.db:
            self.index.db.execute('CREATE TABLE commit_cache_audits(id TEXT PRIMARY KEY,block INTEGER,policy_version INTEGER,body TEXT)')
            install_commit_revision_tracking(self.index.db)
            self.index.db.execute('INSERT INTO commit_cache_audits VALUES(?,?,?,?)',('other',1,1,
                canonical(dict(transaction_signatures_reverified=True,policy_transactions=[999]))))
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'legacy_unchecked')
        with self.index.db:self.index.db.execute('INSERT INTO commit_cache_audits VALUES(?,?,?,?)',('matched',1,1,
            canonical(dict(transaction_signatures_reverified=True,policy_transactions=[1]))))
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'committed')
    def test_new_policy_revisits_unchecked_without_altering_old_edges_or_cursors(self):
        block=self.tx(ix=1);ident=self.edge(ix=1)
        original=self.graph.db.execute('SELECT body FROM edges').fetchone()[0]
        first=assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'legacy_unchecked')
        block['transactions'][0]['instruction_commit_evidence']=dict(version=1,committed_inner_orders=[1])
        self.index.ingest(block,dict(car_sha256='a'*64,transaction_signatures_verified=True))
        second=assess_edges(self.graph,self.index,self.scope)
        self.assertNotEqual(first['revision'],second['revision']);self.assertEqual(self.status(ident),'committed')
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0],2)
        self.assertEqual(self.graph.db.execute('SELECT body FROM edges').fetchone()[0],original)
        self.assertEqual(self.graph.db.execute('SELECT cursor FROM frontier').fetchone()[0],'123')
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_checkpoints').fetchone()[0],2)
    def test_rejected_inner_cpi_never_becomes_verified_cashflow(self):
        self.tx(ix=1,policy=[]);ident=self.edge(ix=1)
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'rejected')
    def test_failed_transaction_is_rejected_even_for_top_level(self):
        self.tx(success=False);ident=self.edge()
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'rejected')
    def test_dead_processed_branch_invalidates_prior_commit_assessment(self):
        self.tx(source='ticking_retained');ident=self.edge()
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'committed')
        self.index.dead_slot(250000000)
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'rejected')
    def test_branch_ambiguity_needs_matching_hash_or_block_identity(self):
        self.tx();self.tx(sha='b'*64)
        missing=self.edge(sha=None);pinned=self.edge();wrong=self.edge(index_block_id=2)
        assess_edges(self.graph,self.index,self.scope)
        self.assertEqual(self.status(missing),'legacy_unchecked');self.assertEqual(self.status(pinned),'committed')
        self.assertEqual(self.status(wrong),'legacy_unchecked')
    def test_provider_claims_stay_outside_instruction_policy(self):
        self.tx();ids=[self.edge(kind=k) for k in ('funding_record','provider_creator_claim','dataset_early_buy','later_dataset_create')]
        assess_edges(self.graph,self.index,self.scope)
        self.assertTrue(all(self.status(i)=='not_applicable' for i in ids))
        self.assertTrue(edge_requires_commit(dict(kind='indexed_transfer')))
    def test_activity_also_requires_matching_committed_instruction(self):
        self.tx(ix=1,policy=[1]);ident=self.edge(kind='later_buy',ix=1)
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'committed')
    def test_transfer_fields_cannot_borrow_an_unrelated_committed_instruction(self):
        self.tx();ident=self.edge(amount='999')
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'rejected')
    def test_transaction_order_conflict_rejected(self):
        self.tx();ident=self.edge(tx_order=2)
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'rejected')
    def test_changed_revision_does_not_restart_an_in_progress_bounded_pass(self):
        block=self.tx();ids=[self.edge() for _ in range(3)]
        first=assess_edges(self.graph,self.index,self.scope,limit=1)
        block['transactions'][0]['instruction_commit_evidence']=dict(version=1,committed_inner_orders=[])
        self.index.ingest(block,dict(car_sha256='a'*64,transaction_signatures_verified=True))
        second=assess_edges(self.graph,self.index,self.scope,limit=1)
        self.assertEqual(first['pass_id'],second['pass_id']);self.assertEqual(second['cursor'],2)
        self.assertFalse(second['pass_complete']);self.assertEqual(self.status(ids[0]),'legacy_unchecked')
        third=assess_edges(self.graph,self.index,self.scope,limit=1);self.assertTrue(third['pass_complete'])
        fourth=assess_edges(self.graph,self.index,self.scope,limit=1);self.assertNotEqual(first['pass_id'],fourth['pass_id'])
        self.assertEqual(fourth['cursor'],1)
    def test_new_edges_continue_same_revision_without_staling_prior_assessment(self):
        self.tx();first=self.edge();assess_edges(self.graph,self.index,self.scope)
        second=self.edge();report=assess_edges(self.graph,self.index,self.scope)
        self.assertEqual(report['assessed'],1)
        self.assertEqual(self.status(first),'committed');self.assertEqual(self.status(second),'committed')
    def test_read_only_projection_defaults_missing_assessment_to_unchecked(self):
        raw=self.edge();provider=self.edge(kind='funding_record')
        values=latest_assessments(self.graph,[raw,provider])
        self.assertEqual(values[raw]['status'],'legacy_unchecked');self.assertFalse(values[raw]['assessment_current'])
        self.assertEqual(values[provider]['status'],'not_applicable')
        self.assertIsNone(self.graph.db.execute("SELECT 1 FROM sqlite_master WHERE name='edge_commit_assessments'").fetchone())
    def test_body_change_does_not_reuse_prior_committed_assessment(self):
        self.tx();ident=self.edge();assess_edges(self.graph,self.index,self.scope)
        with self.graph.db:self.graph.db.execute("UPDATE edges SET body=json_set(body,'$.amount','999') WHERE id=?",(ident,))
        value=latest_assessments(self.graph,[ident])[ident]
        self.assertFalse(value['assessment_current']);self.assertEqual(value['status'],'legacy_unchecked')
    def test_read_only_projection_sees_dead_branch_before_next_assessment(self):
        self.graph.out=Path(self.tmp.name);self.tx(source='ticking_retained');ident=self.edge()
        assess_edges(self.graph,self.index,self.scope);self.assertEqual(self.status(ident),'committed')
        self.index.dead_slot(250000000)
        value=latest_assessments(self.graph,[ident])[ident]
        self.assertTrue(value['assessment_current']);self.assertEqual(value['status'],'rejected')
        self.assertTrue(value['read_revalidated'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0],1)
    def test_read_only_projection_sees_success_change_before_next_assessment(self):
        self.graph.out=Path(self.tmp.name);self.tx();ident=self.edge()
        assess_edges(self.graph,self.index,self.scope)
        with self.index.db:self.index.db.execute('UPDATE transactions SET successful=0')
        self.assertEqual(self.status(ident),'rejected')
    def test_unrelated_new_transaction_does_not_hide_revalidated_committed_flow(self):
        self.graph.out=Path(self.tmp.name);self.tx();ident=self.edge()
        assess_edges(self.graph,self.index,self.scope);self.tx(sha='b'*64,n=2)
        with assessment_snapshot(self.graph) as snapshot:
            first=latest_assessments(self.graph,[ident],revision=snapshot)[ident]
            second=latest_assessments(self.graph,[ident],revision=snapshot)[ident]
        self.assertEqual(first['status'],'committed');self.assertTrue(first['read_revalidated'])
        self.assertEqual(first['original_assessment_id'],1);self.assertEqual(first,second)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0],1)
    def test_new_policy_version_cannot_reuse_old_committed_inner_proof(self):
        self.graph.out=Path(self.tmp.name);self.tx(ix=1,policy=[1]);ident=self.edge(ix=1)
        assess_edges(self.graph,self.index,self.scope)
        with self.index.db:self.index.db.execute('INSERT INTO tx_commit_evidence VALUES(?,?,?)',
            (1,2,canonical(dict(version=2,committed_inner_orders=[]))))
        value=latest_assessments(self.graph,[ident])[ident]
        self.assertEqual(value['status'],'legacy_unchecked');self.assertTrue(value['read_revalidated'])
    def test_changed_edge_fields_are_read_rejected_without_ledger_mutation(self):
        self.graph.out=Path(self.tmp.name);self.tx();ident=self.edge()
        assess_edges(self.graph,self.index,self.scope)
        with self.graph.db:self.graph.db.execute("UPDATE edges SET body=json_set(body,'$.amount','999') WHERE id=?",(ident,))
        value=latest_assessments(self.graph,[ident])[ident]
        self.assertEqual(value['status'],'rejected');self.assertTrue(value['read_revalidated'])
    def test_fixture_without_disk_index_keeps_checkpoint_fallback(self):
        self.tx();ident=self.edge();assess_edges(self.graph,self.index,self.scope)
        self.assertFalse(hasattr(self.graph,'out'));self.assertEqual(self.status(ident),'committed')
    def test_guard_failure_does_not_append_partial_assessments(self):
        self.tx();self.edge();scope=NS(guard=Mock(side_effect=[None,None,ValueError('DEADLINE')]))
        with self.assertRaisesRegex(ValueError,'DEADLINE'):assess_edges(self.graph,self.index,scope)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0],0)
        self.assertEqual(self.graph.db.execute('SELECT cursor FROM frontier').fetchone()[0],'123')
    def test_batch_and_projection_bounds(self):
        for limit in (0,1001,True):
            with self.assertRaises(ValueError):assess_edges(self.graph,self.index,self.scope,limit)
        with self.assertRaises(ValueError):latest_assessments(self.graph,[str(i) for i in range(1001)])
    def test_priority_admits_new_transfers_past_unfinished_historical_upper_bound(self):
        self.tx()
        for _ in range(12):self.edge(kind='funding_record')
        historic=assess_edges(self.graph,self.index,self.scope,limit=1)
        self.assertFalse(historic['pass_complete'])
        checkpoints=list(self.graph.db.execute('SELECT * FROM edge_commit_checkpoints'))
        passes=list(self.graph.db.execute('SELECT * FROM edge_commit_passes'))
        ids=[self.edge() for _ in range(4)]
        bodies=list(self.graph.db.execute('SELECT rowid,* FROM edges'))
        result=assess_new_edges(self.graph,self.index,self.scope)
        self.assertEqual(result['assessed'],4);self.assertEqual(result['counts'],{'committed':4})
        self.assertTrue(all(self.status(ident)=='committed' for ident in ids))
        self.assertEqual(list(self.graph.db.execute('SELECT * FROM edge_commit_checkpoints')),checkpoints)
        self.assertEqual(list(self.graph.db.execute('SELECT * FROM edge_commit_passes')),passes)
        self.assertEqual(list(self.graph.db.execute('SELECT rowid,* FROM edges')),bodies)
        self.assertEqual(self.graph.db.execute('SELECT cursor FROM frontier').fetchone()[0],'123')
        next_pass=assess_edges(self.graph,self.index,self.scope,limit=2)
        self.assertEqual(next_pass['pass_id'],historic['pass_id']);self.assertEqual(next_pass['cursor'],3)
    def test_priority_newest_first_excludes_provider_claims_and_is_bounded(self):
        self.tx();old=[self.edge() for _ in range(3)];new=[self.edge() for _ in range(2)]
        self.edge(kind='provider_creator_claim')
        result=assess_new_edges(self.graph,self.index,self.scope,limit=2)
        self.assertEqual(result['edge_ids'],list(reversed(new)))
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0],2)
        self.assertTrue(all(latest_assessments(self.graph,[i])[i]['reason']=='assessment_pending' for i in old))
    def test_priority_first_run_uses_current_raw_proof_without_historical_checkpoint(self):
        self.graph.out=Path(self.tmp.name);self.tx();ident=self.edge()
        assess_new_edges(self.graph,self.index,self.scope)
        value=latest_assessments(self.graph,[ident])[ident]
        self.assertEqual(value['status'],'committed');self.assertTrue(value['assessment_current'])
        self.assertEqual(value['assessment_lane'],'new_edge_priority');self.assertIsNone(value['pass_id'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_checkpoints').fetchone()[0],0)
    def test_priority_restart_does_not_duplicate_assessments(self):
        self.tx();self.edge();first=assess_new_edges(self.graph,self.index,self.scope)
        second=assess_new_edges(self.graph,self.index,self.scope)
        self.assertEqual(first['assessed'],1);self.assertEqual(second['assessed'],0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0],1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_passes').fetchone()[0],0)
    def test_priority_does_not_trust_edge_commit_flag_or_mismatched_fields(self):
        self.tx();wrong=self.edge(amount='999',instruction_commit_status='committed')
        missing=self.edge(sha='b'*64,instruction_commit_status='committed')
        result=assess_new_edges(self.graph,self.index,self.scope)
        values=latest_assessments(self.graph,[wrong,missing],require_current=False)
        self.assertEqual(values[wrong]['status'],'rejected');self.assertEqual(values[wrong]['reason'],'transfer_field_conflict')
        self.assertEqual(values[missing]['status'],'legacy_unchecked')
        self.assertEqual(result['counts'],{'legacy_unchecked':1,'rejected':1})
    def test_priority_applies_failed_cpi_policy(self):
        self.tx(ix=1,policy=[]);ident=self.edge(ix=1,instruction_commit_status='committed')
        assess_new_edges(self.graph,self.index,self.scope)
        value=latest_assessments(self.graph,[ident],require_current=False)[ident]
        self.assertEqual(value['status'],'rejected');self.assertEqual(value['reason'],'cpi_policy_excluded')
    def test_priority_leaves_already_unchecked_for_historical_reassessment(self):
        block=self.tx(ix=1);ident=self.edge(ix=1)
        assess_new_edges(self.graph,self.index,self.scope)
        block['transactions'][0]['instruction_commit_evidence']=dict(version=1,committed_inner_orders=[1])
        self.index.ingest(block,dict(car_sha256='a'*64,transaction_signatures_verified=True))
        self.assertEqual(assess_new_edges(self.graph,self.index,self.scope)['assessed'],0)
        assess_edges(self.graph,self.index,self.scope)
        self.assertEqual(self.status(ident),'committed')
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0],2)
    def test_priority_guard_failure_appends_no_partial_records(self):
        self.tx();self.edge();scope=NS(guard=Mock(side_effect=[None,None,ValueError('DEADLINE')]))
        with self.assertRaisesRegex(ValueError,'DEADLINE'):assess_new_edges(self.graph,self.index,scope)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0],0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edge_commit_checkpoints').fetchone()[0],0)
    def test_priority_rejects_oversized_batch_and_open_writer_transaction(self):
        for limit in (0,65,True):
            with self.assertRaisesRegex(ValueError,'PRIORITY_BOUND'):assess_new_edges(self.graph,self.index,self.scope,limit)
        self.graph.db.execute('BEGIN')
        with self.assertRaisesRegex(ValueError,'OPEN_TRANSACTION'):assess_new_edges(self.graph,self.index,self.scope)
        self.graph.db.rollback()


if __name__=='__main__':unittest.main()
