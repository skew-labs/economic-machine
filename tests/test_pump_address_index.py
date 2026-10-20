import tempfile
from pathlib import Path
import unittest
import struct
from types import SimpleNamespace as NS
from unittest.mock import Mock
from economic_machine.pump_research.address_index import AddressIndex,search,read_indexed_slot,transfer_commit_status
from economic_machine.pump_research.store import encode

def key(n):return encode(bytes([n])*32)
def sig(n):return encode(bytes([n])*64)

class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'index.sqlite3';self.index=AddressIndex(self.path);self.addCleanup(self.index.close)
    def block(self,n,success=True,errors=None,tx_order=0):
        t=dict(signature=sig(n),slot=250000000,index=tx_order,success=success,account_keys=[key(1),key(2),key(3)],
            transfers=[dict(ix=0,source=key(1),destination=key(2),mint=key(3),amount='18446744073709551615',kind='spl_token_account_transfer',authority=key(1))],activity=[],launches=[])
        return dict(slot=250000000,time=1706000000,transactions=[t],transaction_nodes=1+len(errors or []),decode_errors=errors or [])
    def test_loaded_accounts_and_failed_transactions_searchable_no_false_transfer(self):
        self.index.ingest(self.block(1,False),dict(car_sha256='a'*64))
        r=search(self.path,key(3));self.assertEqual(len(r['transactions']),1)
        self.assertEqual(r['transactions'][0]['successful'],0);self.assertEqual(r['transactions'][0]['transfers'],[])
        self.assertFalse(r['complete_address_history'])
    def test_idempotence_and_amount_exact_token_account_not_owner(self):
        self.assertTrue(self.index.ingest(self.block(1),dict(car_sha256='a'*64)))
        self.assertFalse(self.index.ingest(self.block(1),dict(car_sha256='a'*64)))
        r=search(self.path,key(1));self.assertEqual(len(r['transactions']),1)
        t=r['transactions'][0]['transfers'][0];self.assertEqual(t['amount'],'18446744073709551615');self.assertEqual(t['destination'],key(2))
    def test_partial_decode_and_noncanonical_branches_remain_distinct(self):
        self.index.ingest(self.block(1,errors=['bad metadata']),dict(car_sha256='a'*64))
        self.index.ingest(self.block(1),dict(car_sha256='b'*64))
        r=search(self.path,key(1));self.assertEqual(len(r['transactions']),2)
        self.assertEqual({c['state'] for c in r['coverage']},{'decoded','partial_decode'})
        self.assertTrue(all(not t['independent_consensus_verified'] for t in r['transactions']))
    def test_same_slot_pagination_and_empty_never_means_full_history(self):
        for n in range(1,4):self.index.ingest(self.block(n,tx_order=n),dict(car_sha256=str(n)*64))
        first=search(self.path,key(1),limit=2);second=search(self.path,key(1),**first['next_cursor'],limit=2)
        self.assertEqual(len(first['transactions'])+len(second['transactions']),3)
        self.assertFalse(set(t['id'] for t in first['transactions'])&set(t['id'] for t in second['transactions']))
        absent=search(self.path,key(9));self.assertEqual(absent['transactions'],[]);self.assertTrue(absent['unread_ranges_remain'])
    def test_invalid_address_and_partial_block_atomicity(self):
        with self.assertRaises(ValueError):search(self.path,"' OR 1=1")
        b=self.block(1);b['transactions'][0]['transfers'][0]['source']=key(99)
        with self.assertRaises(KeyError):self.index.ingest(b,dict(car_sha256='a'*64))
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM blocks').fetchone()[0],0)
    def test_dead_stream_slot_before_and_after_ingestion_never_revokes_archive(self):
        self.index.dead_slot(250000000)
        self.index.ingest(self.block(1),dict(car_sha256='a'*64,source='ticking_retained',coverage_state='filtered_processed_stream'))
        self.index.ingest(self.block(2),dict(car_sha256='b'*64))
        self.index.dead_slot(250000000)
        r=search(self.path,key(1));self.assertEqual(len(r['transactions']),1)
        self.assertEqual(r['transactions'][0]['signature'],sig(2))
        self.assertEqual(r['transactions'][0]['source'],'reconstructed_public_block')
    def with_owners(self,block,pre=4,post=4):
        tx=block['transactions'][0];tx['precision_version']=2;tx['precision_errors']=[]
        tx['token_owner_snapshots']=[dict(account=key(2),phase=phase,owner=key(owner),mint=key(3),amount='100',decimals=6)
            for phase,owner in [('pre',pre),('post',post)]] if pre is not None and post is not None else []
        return block
    def test_incoming_token_account_owner_is_searchable_without_account_key_membership(self):
        self.index.ingest(self.with_owners(self.block(1)),dict(car_sha256='a'*64))
        tx=search(self.path,key(4))['transactions'][0]
        self.assertFalse(tx['address_match']['account_key_present'])
        self.assertEqual(len(tx['address_match']['token_owner_boundary_observations']),2)
        self.assertFalse(tx['address_match']['instruction_time_owner_proven'])
        self.assertEqual(tx['transfers'][0]['destination'],key(2))
        self.assertEqual(tx['transfers'][0]['destination_owner_evidence']['boundary_owner'],key(4))
    def test_changed_boundary_owners_both_searchable_without_equating_owners(self):
        self.index.ingest(self.with_owners(self.block(1),4,5),dict(car_sha256='a'*64))
        for owner,phase in [(4,'pre'),(5,'post')]:
            tx=search(self.path,key(owner))['transactions'][0]
            self.assertEqual([x['phase'] for x in tx['address_match']['token_owner_boundary_observations']],[phase])
            self.assertEqual(tx['transfers'][0]['destination_owner_evidence']['status'],'changed_owner')
            self.assertFalse(tx['address_match']['same_person_proven'])
    def test_account_and_owner_matches_deduplicated_across_pages(self):
        for n in range(1,4):
            self.index.ingest(self.with_owners(self.block(n),1,1),dict(car_sha256=str(n)*64))
        first=search(self.path,key(1),limit=2);second=search(self.path,key(1),limit=2,**first['next_cursor'])
        rows=first['transactions']+second['transactions']
        self.assertEqual(len(rows),3);self.assertEqual(len({r['id'] for r in rows}),3)
        self.assertTrue(all(r['address_match']['account_key_present'] for r in rows))
    def test_owner_matches_keep_failed_transactions_but_no_transfers_and_exclude_dead_branch(self):
        self.index.ingest(self.with_owners(self.block(1,False)),dict(car_sha256='a'*64))
        self.index.ingest(self.with_owners(self.block(2)),dict(car_sha256='b'*64,source='ticking_retained'))
        self.index.dead_slot(250000000)
        rows=search(self.path,key(4))['transactions'];self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['successful'],0);self.assertEqual(rows[0]['transfers'],[])
    def test_delegate_never_becomes_owner_search_match(self):
        block=self.with_owners(self.block(1));block['transactions'][0]['transfers'][0]['authority']=key(9)
        self.index.ingest(block,dict(car_sha256='a'*64))
        self.assertEqual(search(self.path,key(9))['transactions'],[])
    def test_commit_evidence_is_persisted_and_conflicting_replay_rejected(self):
        block=self.block(1);tx=block['transactions'][0]
        tx['instruction_commit_evidence']=dict(version=1,committed_inner_orders=[],omitted_inner_count=1,errors=['CPI_REVERTED_INVOCATION'])
        self.index.ingest(block,dict(car_sha256='a'*64))
        self.assertEqual(search(self.path,key(1))['transactions'][0]['instruction_commit_evidence'],tx['instruction_commit_evidence'])
        tx['instruction_commit_evidence']=dict(tx['instruction_commit_evidence'],omitted_inner_count=0)
        with self.assertRaisesRegex(ValueError,'INDEX_COMMIT_EVIDENCE_CONFLICT'):
            self.index.ingest(block,dict(car_sha256='a'*64))
    def test_pre_240m_slot_and_genesis_provider_ranges_permitted_without_completeness_claim(self):
        for slot in (0,1,239999999):
            reader=NS(request=Mock(return_value=struct.pack('<QI',0,0)))
            guard=Mock()
            self.assertEqual(read_indexed_slot(reader,slot,self.index,guard),(None,None))
            self.assertEqual(reader.request.call_count,1);guard.assert_called_once_with()
            url,start,end=reader.request.call_args.args
            self.assertTrue(url.endswith(f'/{slot//432000}/epoch-{slot//432000}-slot-ranges.raw'))
            self.assertEqual((start,end),((slot%432000)*12,(slot%432000)*12+11))
    def test_slot_bounds_rejected_before_guard_or_network(self):
        for slot in (-1,1000000001,True,1.0,'1'):
            reader=NS(request=Mock());guard=Mock()
            with self.assertRaisesRegex(ValueError,'INDEX_SLOT_BOUND'):read_indexed_slot(reader,slot,self.index,guard)
            reader.request.assert_not_called();guard.assert_not_called()
    def test_transfer_commit_status_distinguishes_top_level_policy_and_legacy(self):
        evidence=dict(version=1,committed_inner_orders=[1,65538])
        self.assertEqual(transfer_commit_status(0,None),'committed')
        self.assertEqual(transfer_commit_status(65536,None),'committed')
        self.assertEqual(transfer_commit_status(1,evidence),'committed')
        self.assertEqual(transfer_commit_status(65538,evidence),'committed')
        self.assertEqual(transfer_commit_status(2,evidence),'rejected')
        self.assertEqual(transfer_commit_status(1,None),'legacy_unchecked')
    def test_malformed_or_unknown_policy_cannot_accept_inner_transfer(self):
        for evidence in ({},[],dict(version=2,committed_inner_orders=[1]),dict(version=True,committed_inner_orders=[1]),
             dict(version=1),dict(version=1,committed_inner_orders='1'),dict(version=1,committed_inner_orders=[True]),
             dict(version=1,committed_inner_orders=[1,1]),dict(version=1,committed_inner_orders=[0,1])):
            self.assertEqual(transfer_commit_status(1,evidence),'legacy_unchecked')
        for ix in (-1,True,'1',1.0):self.assertEqual(transfer_commit_status(ix,None),'rejected')
    def test_legacy_inner_transfer_is_visible_only_as_omission_count(self):
        block=self.block(1);block['transactions'][0]['transfers'][0]['ix']=1
        self.index.ingest(block,dict(car_sha256='a'*64))
        tx=search(self.path,key(1))['transactions'][0]
        self.assertEqual(tx['transfers'],[])
        self.assertEqual(tx['transfer_commit_omissions'],dict(rejected=0,legacy_unchecked=1))
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM transfers').fetchone()[0],1)
    def test_replayed_policy_filters_old_rejected_rows_without_deleting_them(self):
        block=self.block(1);t=block['transactions'][0];t['transfers'][0]['ix']=1
        t['transfers'].append(dict(t['transfers'][0],ix=2,amount='200'))
        self.index.ingest(block,dict(car_sha256='a'*64))
        t['instruction_commit_evidence']=dict(version=1,committed_inner_orders=[2],omitted_inner_count=1)
        self.assertFalse(self.index.ingest(block,dict(car_sha256='a'*64)))
        tx=search(self.path,key(1))['transactions'][0]
        self.assertEqual([r['ix'] for r in tx['transfers']],[2])
        self.assertEqual(tx['transfer_commit_omissions'],dict(rejected=1,legacy_unchecked=0))
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM transfers').fetchone()[0],2)
    def test_monotonic_commit_revision_tracks_appends_updates_and_deletes(self):
        def revision():return self.index.db.execute('SELECT version FROM commit_source_revision WHERE id=1').fetchone()[0]
        initial=revision();self.index.ingest(self.block(1),dict(car_sha256='a'*64))
        inserted=revision();self.assertGreater(inserted,initial)
        with self.index.db:self.index.db.execute('UPDATE transactions SET successful=0 WHERE id=1')
        updated=revision();self.assertGreater(updated,inserted)
        with self.index.db:self.index.db.execute('INSERT INTO tx_commit_evidence VALUES(1,1,?)',('{"version":1,"committed_inner_orders":[]}',))
        policy=revision();self.assertGreater(policy,updated)
        with self.index.db:self.index.db.execute('DELETE FROM tx_commit_evidence WHERE tx=1')
        self.assertGreater(revision(),policy)

if __name__=='__main__':unittest.main()
