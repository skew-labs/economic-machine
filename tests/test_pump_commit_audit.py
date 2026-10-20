import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
import zlib
from unittest.mock import Mock,patch

from economic_machine.pump_research.address_index import AddressIndex,search
from economic_machine.pump_research.commit_audit import audit_cached_commits
from economic_machine.pump_research.archive import BASE
from economic_machine.pump_research.store import canonical,encode


def key(n):return encode(bytes([n])*32)
def signature(n):return encode(bytes([n])*64)


class CachedCommitAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.cache=self.base/'archive-ranges';self.cache.mkdir()
        self.index=AddressIndex(self.base/'index.sqlite3');self.addCleanup(self.index.close)
        self.raw=b'fixture-CAR-bytes';self.sha=hashlib.sha256(self.raw).hexdigest();self.slot=250000000
        tx=dict(signature=signature(1),slot=self.slot,index=0,success=True,account_keys=[key(1),key(2)],
            transfers=[dict(ix=1,source=key(1),destination=key(2),mint='SOL',amount='100',kind='system_transfer')],
            activity=[],launches=[])
        self.block=dict(slot=self.slot,time=1706000000,transactions=[tx],transaction_nodes=1,decode_errors=[])
        receipt=dict(car_sha256=self.sha,car_offset=10,car_size=len(self.raw))
        self.index.ingest(self.block,receipt)
        epoch=self.slot//432000;url=f'{BASE}/{epoch}/epoch-{epoch}.car'
        ident=hashlib.sha256(f'{url}:10:{10+len(self.raw)-1}'.encode()).hexdigest()
        self.raw_path=self.cache/(ident+'.range');self.raw_path.write_bytes(self.raw)
        self.manifest_path=self.cache/(ident+'.json')
        self.manifest=dict(url=url,start=10,bytes=len(self.raw),sha256=self.sha)
        self.manifest_path.write_text(canonical(self.manifest))
        self.parsed=copy.deepcopy(self.block)
        self.parsed['transactions'][0]['instruction_commit_evidence']=dict(version=1,
            committed_inner_orders=[],committed_inner_count=0,omitted_inner_count=1,errors=['CPI_REVERTED_INVOCATION'])
    def run_audit(self,**kwargs):
        with (patch('economic_machine.pump_research.commit_audit.parse_index_block',return_value=self.parsed),
              patch('economic_machine.pump_research.archive.get',side_effect=AssertionError('NO_NETWORK'))):
            return audit_cached_commits(self.index,self.cache,object(),kwargs.pop('guard',lambda:None),**kwargs)
    def test_cached_reparse_appends_policy_and_keeps_raw_rows_and_receipts(self):
        before=[tuple(r) for r in self.index.db.execute('SELECT * FROM transfers')]
        old_receipt=self.index.db.execute('SELECT receipt FROM blocks').fetchone()[0]
        report=self.run_audit()
        self.assertEqual(report['policies_appended'],1);self.assertEqual(report['network_requests'],0)
        self.assertEqual([tuple(r) for r in self.index.db.execute('SELECT * FROM transfers')],before)
        self.assertEqual(self.index.db.execute('SELECT receipt FROM blocks').fetchone()[0],old_receipt)
        tx=search(self.index.path,key(1))['transactions'][0]
        self.assertEqual(tx['transfers'],[]);self.assertEqual(tx['transfer_commit_omissions']['rejected'],1)
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM commit_cache_audits').fetchone()[0],1)
    def test_identical_replay_appends_no_duplicate_policy_or_audit(self):
        self.run_audit();report=self.run_audit()
        self.assertEqual(report['policies_appended'],0)
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM tx_commit_evidence').fetchone()[0],1)
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM commit_cache_audits').fetchone()[0],1)
    def test_missing_cache_does_not_download_or_validate_legacy_rows(self):
        self.raw_path.unlink();report=self.run_audit()
        self.assertEqual(report['blocks'][0]['state'],'cache_unavailable')
        self.assertEqual(report['policies_appended'],0)
        self.assertEqual(search(self.index.path,key(1))['transactions'][0]['transfer_commit_omissions']['legacy_unchecked'],1)
    def test_raw_hash_mismatch_rejected_before_policy_append(self):
        self.raw_path.write_bytes(b'x'*len(self.raw))
        with self.assertRaisesRegex(ValueError,'COMMIT_AUDIT_CACHE_HASH'):self.run_audit()
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM tx_commit_evidence').fetchone()[0],0)
    def test_manifest_cannot_point_at_another_car_range(self):
        self.manifest['start']=11;self.manifest_path.write_text(canonical(self.manifest))
        with self.assertRaisesRegex(ValueError,'COMMIT_AUDIT_CACHE_MANIFEST'):self.run_audit()
    def test_symlink_cache_refused(self):
        target=self.base/'other';target.write_bytes(self.raw);self.raw_path.unlink();self.raw_path.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'COMMIT_AUDIT_CACHE_SYMLINK'):self.run_audit()
    def test_byte_bound_preserves_resume_position(self):
        report=self.run_audit(max_bytes=len(self.raw)-1)
        self.assertEqual(report['next_after_id'],0);self.assertTrue(report['byte_bound_reached'])
        self.assertTrue(report['more_blocks']);self.assertEqual(report['cache_bytes_read'],0)
    def test_reparsed_transaction_order_conflict_rejected(self):
        self.parsed['transactions'][0]['index']=1
        with self.assertRaisesRegex(ValueError,'COMMIT_AUDIT_TRANSACTION_CONFLICT'):self.run_audit()
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM tx_commit_evidence').fetchone()[0],0)
    def test_newly_decoded_transactions_are_reported_but_not_added(self):
        another=copy.deepcopy(self.parsed['transactions'][0]);another['signature']=signature(2);another['index']=1
        self.parsed['transactions'].append(another);self.parsed['transaction_nodes']=2
        report=self.run_audit()
        self.assertEqual(report['blocks'][0]['unindexed_transactions'],1)
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM transactions').fetchone()[0],1)
    def test_guard_abort_before_commit_leaves_policy_absent(self):
        guard=Mock(side_effect=[None,None,ValueError('DEADLINE')])
        with self.assertRaisesRegex(ValueError,'DEADLINE'):self.run_audit(guard=guard)
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM tx_commit_evidence').fetchone()[0],0)
    def original_object(self,compressed=False):
        root=self.base/'data';(root/'objects').mkdir(parents=True)
        name=self.sha+('.z' if compressed else '')
        (root/'objects'/name).write_bytes(zlib.compress(self.raw) if compressed else self.raw)
        receipt=dict(car_sha256=self.sha,source='reconstructed_public_block',
            locator='original_research_object:'+self.sha,transaction_signatures_verified=True)
        with self.index.db:self.index.db.execute('UPDATE blocks SET receipt=?',(canonical(receipt),))
        return root,canonical(receipt)
    def test_original_object_receipt_without_range_offsets_is_reparsed(self):
        root,receipt=self.original_object();report=self.run_audit(object_roots=(root,))
        self.assertEqual(report['policies_appended'],1)
        self.assertEqual(self.index.db.execute('SELECT receipt FROM blocks').fetchone()[0],receipt)
    def test_compressed_original_object_uses_bounded_hash_checked_store(self):
        root,_=self.original_object(compressed=True)
        report=self.run_audit(object_roots=(root,),max_bytes=32*1024**2)
        self.assertEqual(report['policies_appended'],1)
        self.assertEqual(report['cache_bytes_read'],len(self.raw))
    def test_original_locator_cannot_choose_different_object(self):
        root,_=self.original_object()
        receipt=dict(car_sha256=self.sha,locator='original_research_object:'+'a'*64)
        with self.index.db:self.index.db.execute('UPDATE blocks SET receipt=?',(canonical(receipt),))
        with self.assertRaisesRegex(ValueError,'COMMIT_AUDIT_OBJECT_LOCATOR'):self.run_audit(object_roots=(root,))
    def test_compressed_original_bound_does_not_advance_cursor(self):
        root,_=self.original_object(compressed=True);report=self.run_audit(object_roots=(root,),max_bytes=1024)
        self.assertEqual(report['next_after_id'],0);self.assertTrue(report['byte_bound_reached'])
    def test_raw_only_cursor_skips_processed_blocks_without_marking_them_verified(self):
        with self.index.db:
            for number in range(2,8):
                self.index.db.execute('INSERT INTO blocks(id,slot,hash,state,receipt) VALUES(?,?,?,?,?)',
                    (number,self.slot+number,str(number)*64,'filtered_processed_stream',canonical(dict(source='ticking_retained'))))
        report=self.run_audit(limit=1)
        self.assertEqual(report['scanned_blocks'],1);self.assertFalse(report['more_blocks'])
        self.assertEqual(report['next_after_id'],1)
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM commit_cache_audits').fetchone()[0],1)


if __name__=='__main__':unittest.main()
