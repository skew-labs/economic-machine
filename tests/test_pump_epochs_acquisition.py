import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace as NS,MethodType
import unittest
from unittest.mock import Mock,patch
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'products/pump_research'))
from epochs_download import EpochDownloader,parse_catalog,source_url,checked_range,CHUNK
from epochs_import import inspect_and_import,publish_registry,verify_members,targets


class Response(io.BytesIO):
    def __init__(self,data=b'',status=200,**headers):super().__init__(data);self.status=status;self.headers=headers


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.reservations=[]
        def reserve(size,purpose):
            self.assertGreater(size,0);self.assertLessEqual(size,64*1024**2)
            self.reservations.append((size,purpose));return len(self.reservations)
        self.scope=NS(hash='a'*64,deadline=time.time_ns()//1000000+60000,
            grant=dict(priority_epochs=[1009,1008],additional_io_bytes=128*1024**3,epoch_scope='all_public_solana_history'),
            reserve=reserve,reserved=lambda:sum(r[0] for r in self.reservations),guard=Mock())
        self.opener=Mock();self.worker=EpochDownloader(self.base,self.scope,self.opener);self.addCleanup(self.worker.close)
    def source(self,size=8):return dict(url=source_url(1009),size=size,etag=None,last_modified=None,supports_ranges=True)
    def retained_source(self,size=8):
        p=self.worker.folder(1009)/'source.json';p.write_text(json.dumps(self.source(size)))
    def range(self,data,start,total=8):return Response(data,206,**{'Content-Range':f'bytes {start}-{start+len(data)-1}/{total}'})
    def continuation(self):
        self.scope.continuation=NS(hash='e'*64,parent_expansion_hash=self.scope.hash,assert_epoch=Mock(),effective_deadline_ms=None,
            inventory_epochs=frozenset([685,700,1008,1009]))
        self.scope.effective_deadline_ms=None;self.scope.remaining_io=lambda:None
        return self.scope.continuation
    def test_continuation_resume_preserves_old_identity_and_chunk_rows(self):
        self.retained_source();self.opener.open.return_value=self.range(b'abcd',0)
        self.worker.chunk(1009,self.source(),0,4,hashlib.sha256())
        before=[tuple(r) for r in self.worker.db.execute('SELECT * FROM chunks')]
        requests=[tuple(r) for r in self.worker.db.execute('SELECT * FROM requests')]
        authority=self.continuation();resumed=EpochDownloader(self.base,self.scope,self.opener);self.addCleanup(resumed.close)
        self.assertEqual([r[0] for r in resumed.db.execute('SELECT hash FROM identity')],['a'*64])
        self.assertEqual([tuple(r) for r in resumed.db.execute('SELECT * FROM chunks')],before)
        self.opener.open.return_value=self.range(b'efgh',4)
        with patch('epochs_download.CHUNK',4):resumed.download(1009)
        self.assertEqual([tuple(r) for r in resumed.db.execute('SELECT * FROM requests WHERE id=1')],requests)
        self.assertEqual(resumed.db.execute('SELECT hash FROM execution_authorities').fetchone()[0],authority.hash)
        mappings=[tuple(r) for r in resumed.db.execute('SELECT * FROM request_authorities ORDER BY request_id')]
        self.assertEqual(mappings,[(1,'a'*64,'evidence/solana-expansion-v2/io.sqlite3'),
            (2,'e'*64,'evidence/solana-continuation-v1/io.sqlite3')])
        self.assertEqual(self.opener.open.call_count,2)
    def test_continuation_cannot_rebind_existing_planner(self):
        self.continuation().parent_expansion_hash='f'*64
        with self.assertRaisesRegex(ValueError,'CONTINUATION_PARENT'):EpochDownloader(self.base,self.scope,self.opener)
        self.assertEqual(self.worker.db.execute('SELECT hash FROM identity').fetchone()[0],'a'*64)
    def test_none_deadline_requires_continuation(self):
        self.scope.effective_deadline_ms=None
        with self.assertRaisesRegex(ValueError,'CONTINUATION_REQUIRED'):EpochDownloader(self.base,self.scope,self.opener)
    def test_continuation_uses_effective_io_without_changing_old_cap(self):
        self.retained_source();self.continuation();self.scope.grant['additional_io_bytes']=1
        self.opener.open.return_value=self.range(b'abcdefgh',0)
        self.worker.download(1009)
        self.assertEqual(self.scope.grant['additional_io_bytes'],1)
        status=self.worker.snapshot();self.assertIsNone(status['effective_deadline_ms'])
        self.assertEqual(status['original_deadline_ms'],self.scope.deadline)
        self.assertEqual(status['continuation_grant_sha256'],'e'*64)
    def test_finite_io_allowance_still_prevents_download(self):
        self.retained_source();self.scope.remaining_io=lambda:4
        with self.assertRaisesRegex(ValueError,'ADDITIONAL_IO_LIMIT'):self.worker.download(1009)
        self.opener.open.assert_not_called()
    def test_continuation_keeps_capacity_guard(self):
        self.retained_source();self.continuation()
        self.scope.guard.side_effect=lambda extra=0,**kw:(_ for _ in ()).throw(ValueError('CORE_HOST_FREE_SPACE')) if extra else None
        with self.assertRaisesRegex(ValueError,'HOST_FREE_SPACE'):self.worker.download(1009)
        self.opener.open.assert_not_called();self.assertEqual(self.reservations,[])
    def test_outside_inventory_download_and_reservation_never_dispatch(self):
        authority=self.continuation();authority.assert_epoch.side_effect=ValueError('OUTSIDE_INVENTORY')
        with self.assertRaisesRegex(ValueError,'OUTSIDE_INVENTORY'):self.worker.download(1010)
        with self.assertRaisesRegex(ValueError,'OUTSIDE_INVENTORY'):self.worker.describe(1010)
        with self.assertRaisesRegex(ValueError,'OUTSIDE_INVENTORY'):self.worker.reserve(1010,0,8,'range')
        self.opener.open.assert_not_called();self.assertEqual(self.reservations,[])
        self.assertFalse((self.worker.out/'epochs/1010').exists())
    def test_outside_inventory_demands_stay_visible_but_not_pending(self):
        self.continuation();out=self.base/'evidence/wallet-map-v1';out.mkdir(parents=True)
        c=sqlite3.connect(out/'graph.sqlite3');c.execute('CREATE TABLE frontier(slot INTEGER,root TEXT,wallet TEXT)')
        c.execute('INSERT INTO frontier VALUES(?,?,?)',(1010*432000,'r','w'));c.commit();c.close()
        self.worker.demand();value=self.worker.snapshot()
        self.assertEqual(value['outside_continuation_inventory_epochs'],[1010])
        self.assertNotIn(1010,value['pending_epochs']);self.assertIsNone(value['deadline_ms'])
        self.assertEqual(value['original_deadline_ms'],self.scope.deadline)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM demands WHERE epoch=1010').fetchone()[0],1)
    def test_continuation_cannot_fetch_new_unpinned_catalogue(self):
        self.continuation()
        with self.assertRaisesRegex(ValueError,'CONTINUATION_CATALOGUE_REQUIRED'):self.worker.catalogue()
        self.opener.open.assert_not_called();self.assertEqual(self.reservations,[])
    def test_inventory_exhaustion_never_claims_whole_history(self):
        raw=b'| 1009 | [car](https://files.old-faithful.net/1009/epoch-1009.car) |\n| 1008 | missing |'
        self.opener.open.return_value=Response(raw);self.worker.catalogue();self.worker.state(1009,'IMPORTED')
        value=self.worker.snapshot()
        self.assertTrue(value['inventory_exhausted']);self.assertEqual(value['unavailable_epochs'],[1008])
        self.assertFalse(value['whole_wallet_history']);self.assertEqual(value['history_coverage'],'NOT_PROVEN_COMPLETE')
    def test_catalogue_records_missing_ongoing_and_available_without_gsfa_claim(self):
        raw=b'|1058|ongoing|-|\n| 1009 | [car](https://files.old-faithful.net/1009/epoch-1009.car) |\n| 1008 | missing |'
        rows=parse_catalog(raw)
        self.assertEqual([r['provider_state'] for r in rows],['ongoing','available','missing'])
        self.assertTrue(all(r['gsfa_available_unverified'] for r in rows))
        with self.assertRaises(ValueError):parse_catalog(raw+b'\n| 1009 | missing |')
    def test_bounds_and_ranges_are_exact(self):
        for epoch in (-1,True,100001):
            with self.assertRaises(ValueError):source_url(epoch)
        checked_range('bytes 4-7/8',4,4,8)
        for value in ['bytes 0-7/8','bytes 4-7/9',None]:
            with self.assertRaises(ValueError):checked_range(value,4,4,8)
    def test_demand_queue_preserves_beyond_priority_epochs(self):
        out=self.base/'evidence/wallet-map-v1';out.mkdir(parents=True)
        c=sqlite3.connect(out/'graph.sqlite3');c.execute('CREATE TABLE frontier(slot INTEGER,root TEXT,wallet TEXT)')
        c.executemany('INSERT INTO frontier VALUES(?,?,?)',[(700*432000,'r','w'),(0,'discovery','w')]);c.commit();c.close()
        self.worker.demand();self.assertEqual(self.worker.pending(),[1009,1008,700])
        self.worker.state(1009,'IMPORTED');self.worker.demand()
        self.assertEqual(self.worker.pending(),[1008,700])
    def test_legacy_epoch_is_reused_even_when_demand_precedes_catalogue(self):
        out=self.base/'evidence/wallet-map-v1';out.mkdir(parents=True)
        c=sqlite3.connect(out/'graph.sqlite3');c.execute('CREATE TABLE frontier(slot INTEGER,root TEXT,wallet TEXT)')
        c.execute('INSERT INTO frontier VALUES(?,?,?)',(685*432000,'r','w'));c.commit();c.close()
        self.worker.demand();self.opener.open.return_value=Response(b'| 685 | [car](https://files.old-faithful.net/685/epoch-685.car) |')
        self.worker.catalogue();self.assertNotIn(685,self.worker.pending())
        self.assertEqual(self.worker.db.execute('SELECT state FROM epochs WHERE epoch=685').fetchone()[0],'LEGACY_IMPORT_REUSED')
        self.opener.reset_mock()
        with self.assertRaisesRegex(ValueError,'LEGACY_IMPORT_PRESERVED'):self.worker.describe(685)
        self.opener.open.assert_not_called()
    def test_completed_chunks_resume_without_refetch_and_combined_hash_matches(self):
        self.retained_source();self.opener.open.side_effect=[self.range(b'abcd',0),self.range(b'efgh',4)]
        first=self.worker.chunk(1009,self.source(),0,4,hashlib.sha256())
        self.assertEqual(first.hexdigest(),hashlib.sha256(b'abcd').hexdigest())
        with patch('epochs_download.CHUNK',4):manifest=self.worker.download(1009)
        saved=json.loads(manifest.read_bytes());self.assertEqual(saved['sha256'],hashlib.sha256(b'abcdefgh').hexdigest())
        self.assertEqual(self.opener.open.call_count,2);self.assertEqual([r[0] for r in self.reservations],[5,5])
        self.assertFalse(saved['address_index_usable']);self.assertFalse(saved['publisher_checksum_verified'])
    def test_chunk_reserves_before_request_and_overflow_is_included(self):
        def opened(req,**kwargs):
            self.assertEqual(self.reservations[-1][0],5);return self.range(b'abcd',0)
        self.opener.open.side_effect=opened
        self.worker.chunk(1009,self.source(),0,4,hashlib.sha256())
        self.assertEqual(self.worker.db.execute('SELECT state FROM requests').fetchone()[0],'complete')
    def test_wrong_range_never_admits_chunk(self):
        self.opener.open.return_value=Response(b'abcd',206,**{'Content-Range':'bytes 0-3/9'})
        with self.assertRaisesRegex(ValueError,'RANGE_IDENTITY'):self.worker.chunk(1009,self.source(),0,4,hashlib.sha256())
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM chunks').fetchone()[0],0)
        self.assertEqual(self.worker.db.execute('SELECT state FROM requests').fetchone()[0],'failed_retained')
    def test_truncated_part_and_reservation_are_preserved(self):
        self.opener.open.return_value=Response(b'ab',206,**{'Content-Range':'bytes 0-3/8'})
        with self.assertRaises(EOFError):self.worker.chunk(1009,self.source(),0,4,hashlib.sha256())
        p=next((self.worker.folder(1009)/'chunks').glob('*.part'))
        self.assertEqual(p.read_bytes(),b'ab');self.assertEqual(self.scope.reserved(),5)
        self.assertEqual(self.worker.retained(1009)[0],0)
    def test_corrupt_retained_chunk_fails_without_network(self):
        self.opener.open.return_value=self.range(b'abcd',0)
        self.worker.chunk(1009,self.source(),0,4,hashlib.sha256());self.opener.reset_mock()
        next((self.worker.folder(1009)/'chunks').glob('*.range')).write_bytes(b'bad!')
        with self.assertRaisesRegex(ValueError,'CHUNK_HASH'):self.worker.retained(1009)
        self.opener.open.assert_not_called()
    def test_capacity_failure_prevents_archive_download(self):
        self.retained_source()
        self.scope.guard.side_effect=lambda extra=0,**kw:(_ for _ in ()).throw(ValueError('CORE_TOTAL_STORAGE_LIMIT')) if extra else None
        with self.assertRaisesRegex(ValueError,'TOTAL_STORAGE'):self.worker.download(1009)
        self.opener.open.assert_not_called();self.assertEqual(self.reservations,[])
    def test_each_completed_chunk_publishes_progress(self):
        self.retained_source();self.opener.open.side_effect=[self.range(b'abcd',0),self.range(b'efgh',4)]
        with patch('epochs_download.CHUNK',4):self.worker.download(1009)
        status=json.loads((self.worker.out/'status.json').read_bytes())
        self.assertEqual(status['downloaded_epochs'],1)
        self.assertEqual(status['states']['DOWNLOADED_PENDING_INSPECTION'],1)
    def test_cooldown_does_not_reserve_and_success_spacing_is_not_retry_after(self):
        self.worker.cooldown.parent.mkdir(parents=True,exist_ok=True)
        self.worker.cooldown.write_text(json.dumps(dict(last_status=429,not_before=time.time()+120)))
        self.scope.guard.side_effect=ValueError('DEADLINE')
        with self.assertRaisesRegex(ValueError,'DEADLINE'):self.worker.describe(1009)
        self.assertEqual(self.reservations,[]);self.opener.open.assert_not_called()
        self.scope.guard.side_effect=None
        self.worker.cooldown.write_text(json.dumps(dict(last_status=206,not_before=time.time()+120)))
        with patch('epochs_download.time.sleep') as sleep:self.worker.wait_source()
        sleep.assert_not_called()
    def test_retry_after_never_shortens_existing_backoff(self):
        until=time.time()+1000;self.worker.catalogue_cooldown.write_text(json.dumps(dict(last_status=429,not_before=until)))
        error=urllib.error.HTTPError(source_url(1009),429,'limited',{'Retry-After':'700'},None)
        self.worker.backoff(error)
        self.assertGreaterEqual(json.loads(self.worker.catalogue_cooldown.read_bytes())['not_before'],until)
        self.assertFalse(self.worker.cooldown.exists())
    def test_head_transient_retry_keeps_failed_reservation(self):
        self.opener.open.side_effect=[urllib.error.HTTPError(source_url(1009),503,'busy',{'Retry-After':'60'},None),Response(**{'Content-Length':'8','Accept-Ranges':'bytes'})]
        with patch('epochs_download.ArchiveGate') as gate:
            value=self.worker.describe(1009)
        self.assertEqual(value['size'],8);self.assertEqual(len(self.reservations),2);self.assertEqual(gate.call_count,2)
        self.assertEqual([r[0] for r in self.worker.db.execute('SELECT state FROM requests ORDER BY id')],['failed_retained','complete'])
    def test_catalogue_transient_retries_then_freezes_provenance(self):
        raw=b'| 1009 | [car](https://files.old-faithful.net/1009/epoch-1009.car) |'
        self.opener.open.side_effect=[urllib.error.HTTPError('url',429,'busy',{'Retry-After':'60'},None),Response(raw)]
        with patch.object(self.worker,'wait_source'),patch.object(self.worker,'backoff'):
            first=self.worker.catalogue()
        self.opener.reset_mock();second=self.worker.catalogue()
        self.assertEqual(first,second);self.opener.open.assert_not_called()
    def test_catalogue_recovers_hash_matched_raw_saved_before_receipt(self):
        raw=b'| 1009 | [car](https://files.old-faithful.net/1009/epoch-1009.car) |'
        digest=hashlib.sha256(raw).hexdigest();p=self.worker.out/f'catalogue-{digest}.md';p.write_bytes(raw)
        self.opener.open.return_value=Response(raw);saved=self.worker.catalogue()
        self.assertEqual(saved['sha256'],digest);self.assertEqual(p.read_bytes(),raw)
    def test_import_query_can_use_later_observed_wallet_without_history_claim(self):
        out=self.base/'evidence/wallet-map-v1';out.mkdir(parents=True)
        c=sqlite3.connect(out/'graph.sqlite3');c.execute('CREATE TABLE frontier(slot INTEGER,wallet TEXT,source TEXT,depth INTEGER)')
        wallet='71ffD9ttArPfug8gyDqsraCYh2ywAtRMZkQh2tSBj4ie'
        c.execute('INSERT INTO frontier VALUES(?,?,?,?)',(1009*432000,wallet,'indexed_forward',0));c.commit();c.close()
        self.assertEqual(targets(self.base,700),[wallet])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.folder=self.base/'evidence/address-epochs-v2/epochs/1009';self.folder.mkdir(parents=True)
        self.manifest=self.folder/'download-manifest.json';self.manifest.write_text(json.dumps(dict(sha256='b'*64)))
        self.scope=NS(hash='a'*64,deadline=time.time_ns()//1000000+60000,
            grant=dict(epoch_scope='all_public_solana_history'),guard=Mock())
        self.files=[dict(name=n,bytes=1,sha256=hashlib.sha256(b'x').hexdigest()) for n in ['manifest','linked-log','pubkey-to-offset-and-size.index']]
        self.inspected=dict(files=self.files,member_bytes=3,decompressed_tar_bytes=2048,compressed_sha256='b'*64,
            format_inspected=True,downloaded_chunk_hashes_verified=True,extracted=False)
        self.index=Mock();self.index.identity='c'*64;self.index.root_cid='d'*72;self.index.page.return_value=dict(candidates=[])
        self.index.__enter__=Mock(return_value=self.index);self.index.__exit__=Mock(return_value=False)
    def inspect(self,manifest,budget,destination=None):
        if destination is not None:
            destination.mkdir()
            for row in self.files:(destination/row['name']).write_bytes(b'x')
        return dict(self.inspected,extracted=destination is not None)
    def run_import(self):
        with patch('epochs_import.inspect_download',side_effect=self.inspect) as inspect,patch('epochs_import.targets',return_value=['wallet']),patch('epochs_import.GsfaIndex',return_value=self.index):
            result=inspect_and_import(self.base,1009,self.scope,self.manifest)
        return result,inspect
    def continuation(self):
        self.scope.continuation=NS(hash='e'*64,parent_expansion_hash=self.scope.hash,assert_epoch=Mock(),effective_deadline_ms=None)
        self.scope.effective_deadline_ms=None
        self.scope.guard=MethodType(lambda scope,extra=0,force=False:None,self.scope)
        return self.scope.continuation
    def test_complete_inspection_precedes_extraction_and_registry_publication(self):
        result,inspect=self.run_import();self.assertEqual(inspect.call_count,2)
        self.assertEqual(len(inspect.call_args_list[0].args),2);self.assertEqual(len(inspect.call_args_list[1].args),3)
        registry=json.loads((self.base/'evidence/address-epochs-v2/registry.json').read_bytes())
        entry=registry['entries'][0];self.assertEqual(entry['epoch'],1009)
        self.assertEqual(entry['receipt_sha256'],hashlib.sha256(result.read_bytes()).hexdigest())
        self.assertEqual(entry['grant_path'],'evidence/solana-expansion-v2/grant.json')
    def test_inspected_expansion_capacity_blocks_extraction_and_registry(self):
        self.scope.guard.side_effect=lambda extra=0,**kw:(_ for _ in ()).throw(ValueError('CAPACITY')) if extra else None
        with patch('epochs_import.inspect_download',side_effect=self.inspect) as inspect:
            with self.assertRaisesRegex(ValueError,'CAPACITY'):inspect_and_import(self.base,1009,self.scope,self.manifest)
        self.assertEqual(inspect.call_count,1);self.assertTrue((self.folder/'inspection.json').exists())
        self.assertFalse((self.folder/'gsfa-index-1009').exists());self.assertFalse((self.base/'evidence/address-epochs-v2/registry.json').exists())
    def test_full_extraction_receipt_resumes_without_reextracting(self):
        (self.folder/'inspection.json').write_text(json.dumps(self.inspected))
        extracted=self.inspect(None,None,self.folder/'gsfa-index-1009')
        (self.folder/'extraction-receipt.json').write_text(json.dumps(extracted))
        _,inspect=self.run_import();inspect.assert_not_called()
    def test_incomplete_extraction_is_preserved_and_rejected(self):
        (self.folder/'inspection.json').write_text(json.dumps(self.inspected));dest=self.folder/'gsfa-index-1009';dest.mkdir();(dest/'linked-log').write_bytes(b'partial')
        with self.assertRaisesRegex(ValueError,'PARTIAL_IMPORT_PRESERVED'):inspect_and_import(self.base,1009,self.scope,self.manifest)
        self.assertEqual((dest/'linked-log').read_bytes(),b'partial')
    def test_existing_import_rechecks_members_before_registry_republication(self):
        _,_=self.run_import();(self.folder/'gsfa-index-1009/linked-log').write_bytes(b'Y')
        with self.assertRaisesRegex(ValueError,'MEMBER_HASH'):inspect_and_import(self.base,1009,self.scope,self.manifest)
    def test_registry_rejects_unverified_or_legacy_import(self):
        receipt=self.folder/'false.json';receipt.write_text('{}')
        with self.assertRaisesRegex(ValueError,'IMPORT_NOT_VERIFIED'):publish_registry(self.base,1009,self.scope,receipt)
        with self.assertRaisesRegex(ValueError,'LEGACY_REGISTRY_OVERRIDE'):publish_registry(self.base,685,self.scope,receipt)
    def test_registry_existing_entry_cannot_change(self):
        receipt,_=self.run_import();value=json.loads(receipt.read_bytes());value['at_ms']+=1;receipt.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'ENTRY_CHANGED'):publish_registry(self.base,1009,self.scope,receipt)
    def test_no_target_does_not_publish_import_receipt(self):
        with patch('epochs_import.inspect_download',side_effect=self.inspect),patch('epochs_import.targets',return_value=[]),patch('epochs_import.GsfaIndex',return_value=self.index):
            with self.assertRaisesRegex(ValueError,'NO_OBSERVED_TARGET'):inspect_and_import(self.base,1009,self.scope,self.manifest)
        self.assertTrue((self.folder/'extraction-receipt.json').exists());self.assertFalse((self.folder/'import-receipt.json').exists())
    def test_continuation_reuses_import_without_rewriting_original_evidence(self):
        receipt,_=self.run_import();before=receipt.read_bytes()
        registry=self.base/'evidence/address-epochs-v2/registry.json';registry_before=registry.read_bytes()
        self.continuation()
        _,inspect=self.run_import();inspect.assert_not_called()
        self.assertEqual(receipt.read_bytes(),before);self.assertEqual(registry.read_bytes(),registry_before)
        reference=self.folder/'execution-authorities'/('e'*64+'.json')
        value=json.loads(reference.read_bytes());self.assertEqual(value['original_grant_sha256'],'a'*64)
        self.assertIsNone(value['effective_deadline_ms'])
    def test_new_import_accepts_authorized_none_effective_deadline(self):
        self.scope.deadline=1;self.continuation()
        receipt,inspect=self.run_import()
        self.assertTrue(receipt.exists());self.assertEqual(inspect.call_count,2)
        self.assertTrue(all(call.args[1].deadline_ms is None for call in inspect.call_args_list))
    def test_import_without_continuation_keeps_original_deadline(self):
        self.scope.deadline=1
        with self.assertRaisesRegex(ValueError,'ORIGINAL_DEADLINE'):self.run_import()
        self.assertFalse((self.folder/'import-receipt.json').exists())
    def test_continuation_inventory_bound_applies_before_import(self):
        self.continuation().assert_epoch.side_effect=ValueError('CONTINUATION_EPOCH_BOUND')
        with self.assertRaisesRegex(ValueError,'EPOCH_BOUND'):self.run_import()
        self.assertFalse((self.folder/'inspection.json').exists())


if __name__=='__main__':unittest.main()
