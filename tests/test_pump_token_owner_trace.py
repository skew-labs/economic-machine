import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from economic_machine.pump_research.address_index import AddressIndex
from economic_machine.pump_research.store import canonical, encode
from economic_machine.pump_research.token_owner_trace import seed_owner_accounts
from economic_machine.pump_research.wallet_map import Graph


def key(n):
    return encode(n.to_bytes(32, 'big'))


class TokenOwnerTraceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        (self.base/'evidence').mkdir()
        self.graph = Graph(self.base)
        self.addCleanup(self.graph.close)
        self.index = AddressIndex(self.graph.out/'address-index.sqlite3')
        self.addCleanup(self.index.close)
        self.scope = SimpleNamespace(guard=lambda:None)
        self.wallet = key(10)
        self.account = key(11)
        self.mint = key(12)
        self.sequence = 0
        self.root(self.wallet)

    def root(self, wallet, kind='wallet'):
        with self.graph.db:
            self.graph.db.execute('INSERT INTO roots VALUES(?,?)',
                (wallet,canonical(dict(mint=wallet,root_kind=kind,original_marker='keep'))))

    def ingest(self, *, owner=None, account=None, phases=('pre','post'),
               later_owner=None, success=True, dead=False, slot=None, metadata=True,
               transfer=False):
        self.sequence += 1
        owner = owner or self.wallet
        account = account or self.account
        slot = slot or 400000000+self.sequence
        tx = dict(signature=encode(bytes([self.sequence])*64),slot=slot,index=2,success=success,
            account_keys=[account,key(13),self.mint],launches=[],activity=[],
            transfers=[dict(ix=0,source=account,destination=key(13),mint=self.mint,
                amount='21',kind='spl_token_account_transfer',authority=key(99))] if transfer else [],
            instruction_commit_evidence=dict(version=1,committed_inner_orders=[]))
        if metadata:
            tx.update(precision_version=2,precision_errors=[],token_owner_snapshots=[
                dict(account=account,phase=phase,owner=later_owner if phase=='post' and later_owner else owner,
                    mint=self.mint,amount='100',decimals=6) for phase in phases])
        block=dict(slot=slot,time=1706000000+self.sequence,transactions=[tx],transaction_nodes=1,decode_errors=[])
        self.index.ingest(block,dict(car_sha256=('%064x'%self.sequence),
            transaction_signatures_verified=True,source='reconstructed_public_block',
            coverage_state='dead_branch' if dead else 'decoded'))
        return block

    def observations(self):
        return [dict(r, evidence=json.loads(r['body'])) for r in self.graph.db.execute(
            "SELECT * FROM edges WHERE kind='token_account_owner_observation' ORDER BY rowid")]

    def test_boundary_associations_preserve_provenance_and_queue_actual_account(self):
        block=self.ingest()
        result=seed_owner_accounts(self.graph,self.scope)
        self.assertEqual(result['observations_added'],2)
        self.assertEqual(result['account_jobs_added'],1)
        self.assertEqual(result['model_calls'],0)
        for row in self.observations():
            self.assertEqual((row['source'],row['target']),(self.wallet,self.account))
            body=row['evidence']
            self.assertEqual(body['source_sha256'],'%064x'%1)
            self.assertEqual(body['signature'],block['transactions'][0]['signature'])
            self.assertEqual(body['mint'],self.mint)
            self.assertEqual(body['owner_evidence']['status'],'same_owner_at_both_boundaries')
            self.assertEqual(body['boundary_amount_raw'],'100')
            self.assertNotIn('amount',body)
            self.assertNotIn('transaction_signature_verified',body)
            self.assertNotIn('raw_wallet_membership_verified',body)
            self.assertTrue(body['source_transaction_signatures_verified'])
            for flag in ('transfer_or_payment','instruction_time_owner_proven','current_owner_verified',
                         'same_person_proven','same_funds_continuity_proven','independent_consensus_verified'):
                self.assertFalse(body[flag])
        job=self.graph.db.execute('SELECT * FROM frontier').fetchone()
        self.assertEqual((job['root'],job['wallet'],job['source'],job['depth']),
                         (self.wallet,self.account,'indexed_forward',1))
        self.assertEqual(job['slot'],block['slot'])
        self.assertEqual(job['cursor'],'null')
        self.assertIsNone(self.graph.anchor(job))

    def test_processed_source_metadata_does_not_claim_raw_signature_verification(self):
        self.ingest(phases=('post',))
        with self.index.db:
            receipt=json.loads(self.index.db.execute('SELECT receipt FROM blocks').fetchone()[0])
            receipt.update(source='ticking_retained',transaction_signatures_verified=False)
            self.index.db.execute('UPDATE blocks SET receipt=?',(canonical(receipt),))
        seed_owner_accounts(self.graph,self.scope)
        body=self.observations()[0]['evidence']
        self.assertEqual(body['original_source'],'ticking_retained')
        self.assertFalse(body['source_transaction_signatures_verified'])
        self.assertNotIn('transaction_signature_verified',body)
        self.assertNotIn('raw_wallet_membership_verified',body)
        self.assertFalse(body['transfer_or_payment'])

    def test_changed_and_one_boundary_owner_never_become_instruction_time_owner(self):
        self.ingest(later_owner=key(20))
        self.ingest(account=key(21),phases=('post',))
        seed_owner_accounts(self.graph,self.scope)
        rows=self.observations()
        self.assertEqual(len(rows),2)
        self.assertEqual({r['evidence']['owner_evidence']['status'] for r in rows},
                         {'changed_owner','one_boundary_only'})
        self.assertTrue(all(r['evidence']['owner_evidence']['boundary_owner'] is None for r in rows))
        self.assertTrue(all(not r['evidence']['instruction_time_owner_proven'] for r in rows))
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM frontier WHERE wallet=?',(key(20),)).fetchone()[0],0)

    def test_authority_delegate_without_metadata_is_not_owner(self):
        self.root(key(99))
        self.ingest(metadata=False,transfer=True)
        result=seed_owner_accounts(self.graph,self.scope)
        self.assertEqual(result['observations_added'],0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM frontier').fetchone()[0],0)

    def test_failed_dead_and_token_roots_do_not_seed_accounts(self):
        self.ingest(success=False)
        self.ingest(dead=True)
        self.root(key(30),'token')
        self.ingest(owner=key(30))
        result=seed_owner_accounts(self.graph,self.scope)
        self.assertEqual(result['observations_added'],0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM frontier').fetchone()[0],0)

    def test_replay_preserves_original_roots_cursors_and_job_anchor(self):
        self.ingest()
        with self.graph.db:
            original=self.graph.enqueue(self.wallet,key(40),399999000,None,0,'indexed_forward',after_order=(7,0))
            self.graph.db.execute('UPDATE frontier SET cursor=? WHERE id=?',('{"transfer":99}',original))
        old=self.graph.db.execute('SELECT * FROM frontier WHERE id=?',(original,)).fetchone()
        roots=[tuple(r) for r in self.graph.db.execute('SELECT * FROM roots')]
        seed_owner_accounts(self.graph,self.scope)
        first=[tuple(r) for r in self.graph.db.execute('SELECT * FROM token_owner_scan_receipts')]
        second=seed_owner_accounts(self.graph,self.scope)
        self.assertEqual(second['scanned'],0)
        self.assertEqual(first,[tuple(r) for r in self.graph.db.execute('SELECT * FROM token_owner_scan_receipts')])
        self.assertEqual(tuple(old),tuple(self.graph.db.execute('SELECT * FROM frontier WHERE id=?',(original,)).fetchone()))
        self.assertEqual(roots,[tuple(r) for r in self.graph.db.execute('SELECT * FROM roots')])
        self.assertEqual(self.graph.anchor(old)['tx_order'],7)

    def test_row_budget_paginates_without_losing_two_phases_and_follows_append(self):
        self.ingest()
        first=seed_owner_accounts(self.graph,self.scope,limit=1)
        second=seed_owner_accounts(self.graph,self.scope,limit=1)
        self.assertEqual((first['scanned'],second['scanned']),(1,1))
        self.assertEqual(len(self.observations()),2)
        self.ingest(phases=('post',),slot=400000010)
        third=seed_owner_accounts(self.graph,self.scope,limit=1)
        self.assertEqual(third['observations_added'],1)
        self.assertEqual(third['index_transaction_upper_bound'],2)
        self.assertFalse(third['complete_wallet_history'])
        self.assertFalse(third['late_enrichment_below_cursor_reconciled'])

    def test_bounded_rotation_does_not_starve_second_wallet(self):
        self.root(key(20))
        self.ingest(owner=self.wallet)
        self.ingest(owner=key(20),account=key(25))
        seed_owner_accounts(self.graph,self.scope,limit=1)
        seed_owner_accounts(self.graph,self.scope,limit=1)
        self.assertEqual({r['root'] for r in self.observations()},{self.wallet,key(20)})

    def test_failure_rolls_back_observation_job_and_scan_cursor(self):
        self.ingest()
        with patch.object(self.graph,'enqueue',side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError,'injected'):
                seed_owner_accounts(self.graph,self.scope)
        self.assertEqual(self.observations(),[])
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM token_owner_scan').fetchone()[0],0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM token_owner_scan_receipts').fetchone()[0],0)
        self.assertEqual(seed_owner_accounts(self.graph,self.scope)['observations_added'],2)

    def test_invalid_metadata_is_audited_without_blocking_later_rows(self):
        self.ingest(account=key(15))
        self.ingest(account=key(16),phases=('post',))
        with self.index.db:
            self.index.db.execute("UPDATE token_owners SET mint='invalid' WHERE tx=1")
        result=seed_owner_accounts(self.graph,self.scope)
        self.assertEqual(result['skipped'],2)
        self.assertEqual(result['observations_added'],1)
        self.assertEqual(result['skip_reasons'],{'TOKEN_OWNER_METADATA_IDENTITY':2})

    def test_actual_token_account_flow_is_not_rewritten_as_owner_payment(self):
        first=self.ingest(phases=('post',),transfer=True)
        seed_owner_accounts(self.graph,self.scope)
        self.ingest(phases=(),transfer=True,slot=first['slot']+1)
        job=dict(self.graph.db.execute('SELECT * FROM frontier').fetchone())
        result=self.graph.indexed_forward(job,self.scope)
        self.assertEqual(result['indexed_transfers'],1)
        row=self.graph.db.execute("SELECT * FROM edges WHERE kind='indexed_transfer'").fetchone()
        self.assertEqual((row['source'],row['target']),(self.account,key(13)))
        self.assertNotEqual(row['source'],self.wallet)
        self.assertEqual(json.loads(row['body'])['ordering'],'later_slot_only')
        self.assertFalse(json.loads(row['body'])['path_funds_continuity_proven'])

    def test_scope_and_open_transaction_fail_before_source_cursor_changes(self):
        self.ingest()
        with self.assertRaisesRegex(ValueError,'scope-expired'):
            seed_owner_accounts(self.graph,SimpleNamespace(guard=lambda:(_ for _ in ()).throw(ValueError('scope-expired'))))
        with self.assertRaisesRegex(ValueError,'TOKEN_OWNER_BATCH_BOUND'):
            seed_owner_accounts(self.graph,self.scope,limit=0)
        self.graph.db.execute('BEGIN')
        try:
            with self.assertRaisesRegex(ValueError,'TOKEN_OWNER_OPEN_TRANSACTION'):
                seed_owner_accounts(self.graph,self.scope)
        finally:
            self.graph.db.rollback()


if __name__=='__main__':
    unittest.main()
