import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import test_pump_portal_history as fixtures
from test_pump_portal_history import ROOT,WALLET,OTHER,SLOT,SIG,line
from economic_machine.pump_research.portal_history import PortalHistory,MAX_RESPONSE,SOURCE_BYTES,query,sha
from economic_machine.pump_research.store import canonical


class ContinuationTests(unittest.TestCase):
    setUp=fixtures.AdapterTests.setUp
    job=fixtures.AdapterTests.job
    mocked_fetch=fixtures.AdapterTests.mocked_fetch

    def enable(self):
        epochs=frozenset(range(1000))
        def assert_slot(slot):
            if type(slot) is not int or slot<0 or slot//432000 not in epochs:
                raise ValueError('CORE_CONTINUATION_SLOT_BOUND')
        value=SimpleNamespace(hash='d'*64,effective_deadline_ms=None,inventory_epochs=epochs,assert_slot=assert_slot)
        self.scope.continuation=value;self.scope.effective_deadline_ms=None
        def reserve(n,p):self.reserved.append((n,p));return len(self.reserved)
        self.scope.reserve=reserve
        return value

    def history_job(self):
        self.adapter.maintain_history(self.scope)
        return dict(self.g.db.execute('SELECT f.* FROM portal_history_windows w JOIN frontier f ON f.id=w.job ORDER BY w.rowid DESC LIMIT 1').fetchone())

    def metadata(self,floor):
        raw=canonical(dict(dataset='solana-mainnet',start_block=floor)).encode();digest=sha(raw)
        path=self.g.out/'metadata-test.raw';path.write_bytes(raw)
        receipt=dict(schema='economic-machine.portal-dataset-metadata.v1',start_block=floor,
            raw_path=str(path.relative_to(self.base)),raw_sha256=digest,observed_ms=1,
            endpoint='https://portal.sqd.dev/datasets/solana-mainnet/metadata')
        (self.g.out/'portal-dataset-metadata.json').write_text(canonical(receipt))
        return path,receipt

    def test_additive_io_preserves_exhausted_old_journal(self):
        with self.g.db:self.g.db.execute('INSERT INTO portal_source_io VALUES(1,?,?,?,?,?)',
            (SOURCE_BYTES,'old',1,'reserved','{}'))
        before=[tuple(r) for r in self.g.db.execute('SELECT * FROM portal_source_io')]
        self.enable();ident=self.adapter._reserve(self.scope,'new')
        self.assertEqual(ident,1)
        self.assertEqual([tuple(r) for r in self.g.db.execute('SELECT * FROM portal_source_io')],before)
        row=self.g.db.execute('SELECT * FROM portal_continuation_io').fetchone()
        self.assertEqual(row['grant_sha256'],'d'*64);self.assertEqual(row['bytes'],MAX_RESPONSE+1)
        self.assertEqual(json.loads(row['body'])['outer_reservation'],1)
        self.assertEqual(PortalHistory(self.g).snapshot()['continuation_query_reserved_bytes'],MAX_RESPONSE+1)

    def test_failed_outer_reservation_stays_in_new_ledger_only(self):
        self.enable();self.scope.reserve=lambda *a:(_ for _ in ()).throw(ValueError('CORE_HOST_FREE_SPACE'))
        with self.assertRaisesRegex(ValueError,'HOST_FREE_SPACE'):self.adapter._reserve(self.scope,'new')
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_continuation_io').fetchone()[0],1)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_source_io').fetchone()[0],0)

    def test_missing_effective_continuation_is_not_unlimited(self):
        self.enable();self.scope.effective_deadline_ms=10
        with self.assertRaisesRegex(ValueError,'CONTINUATION_SCOPE'):self.adapter._reserve(self.scope,'bad')
        self.assertEqual(self.reserved,[])

    def test_empty_forward_appends_successor_without_advancing_old_cursor(self):
        job=self.job();self.enable()
        with patch.object(self.adapter,'fetch',return_value=self.mocked_fetch(b'',200)):
            result=self.adapter.run(job,self.scope)
        self.assertEqual(result['next_slot'],SLOT)
        child=self.g.db.execute('SELECT child FROM portal_gap_successors WHERE parent=?',(job['id'],)).fetchone()[0]
        next_job=self.g.db.execute('SELECT * FROM frontier WHERE id=?',(child,)).fetchone()
        self.assertEqual(json.loads(next_job['cursor'])['next_slot'],SLOT+10000)
        self.assertEqual(json.loads(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0])['next_slot'],SLOT)
        gap=json.loads(self.g.db.execute('SELECT body FROM portal_unresolved_gaps').fetchone()[0])
        self.assertEqual(gap['reason'],'empty_provider_window_200');self.assertFalse(gap['absence_proven'])
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM edges').fetchone()[0],0)

    def test_204_is_head_unavailable_not_empty_wallet_claim(self):
        job=self.job();self.enable()
        with patch.object(self.adapter,'fetch',return_value=self.mocked_fetch(b'',204)):
            self.adapter.run(job,self.scope)
        value=json.loads(self.g.db.execute('SELECT body FROM portal_unresolved_gaps').fetchone()[0])
        self.assertEqual(value['reason'],'provider_head_unavailable_204')
        self.assertFalse(value['whole_wallet_history'])

    def test_old_empty_page_recovery_preserves_page_and_original_cursor(self):
        job=self.job()
        with patch.object(self.adapter,'fetch',return_value=self.mocked_fetch(b'',204)):
            self.adapter.run(job,self.scope)
        cursor=self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0]
        pages=[tuple(r) for r in self.g.db.execute('SELECT * FROM portal_pages')]
        self.enable();self.adapter.maintain_history(self.scope);PortalHistory(self.g).maintain_history(self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_gap_successors').fetchone()[0],1)
        self.assertEqual([tuple(r) for r in self.g.db.execute('SELECT * FROM portal_pages')],pages)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0],cursor)

    def test_one_historical_window_only_and_restart_keeps_pending_job(self):
        self.job();self.enable();job=self.history_job();before=job['cursor']
        PortalHistory(self.g).maintain_history(self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_history_windows').fetchone()[0],1)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0],before)
        window=self.g.db.execute('SELECT * FROM portal_history_windows').fetchone()
        self.assertEqual(window['upper_slot'],SLOT-1);self.assertLessEqual(window['upper_slot']-window['lower_slot'],9999)

    def test_empty_historical_window_moves_only_scheduler_cursor_backward(self):
        self.job();self.enable();job=self.history_job()
        old=self.g.db.execute('SELECT * FROM portal_history_windows WHERE job=?',(job['id'],)).fetchone()
        with patch.object(self.adapter,'fetch',return_value=self.mocked_fetch(b'',200)):
            self.adapter.run(job,self.scope)
        old_cursor=self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0]
        self.adapter.maintain_history(self.scope)
        new=self.g.db.execute('SELECT * FROM portal_history_windows ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(new['upper_slot'],old['lower_slot']-1)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0],old_cursor)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_gap_successors').fetchone()[0],0)

    def test_partial_page_keeps_active_window_and_last_header_cursor(self):
        self.job();self.enable();job=self.history_job();start=json.loads(job['cursor'])['next_slot']
        with patch.object(self.adapter,'fetch',return_value=self.mocked_fetch(line(start,[]))):
            self.adapter.run(job,self.scope)
        self.adapter.maintain_history(self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_history_windows').fetchone()[0],1)
        self.assertEqual(json.loads(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0])['next_slot'],start+1)

    def test_genesis_enumeration_never_means_complete_history(self):
        self.enable();self.adapter.seed_wallet(ROOT,OTHER,self.scope,start=2,end=4)
        job=self.history_job()
        with patch.object(self.adapter,'fetch',return_value=self.mocked_fetch(b'',200)):
            self.adapter.run(job,self.scope)
        self.adapter.maintain_history(self.scope)
        row=self.g.db.execute('SELECT * FROM portal_history_traversals').fetchone()
        self.assertEqual(row['next_window_end'],-1)
        self.assertEqual(row['state'],'inventory_enumerated_with_unverified_gaps')
        self.assertFalse(self.adapter.snapshot()['whole_wallet_history'])

    def test_later_wallet_is_not_starved_by_pending_older_window(self):
        self.job();self.enable();self.history_job()
        self.adapter.seed_wallet(ROOT,OTHER,self.scope,start=SLOT,end=SLOT+5)
        self.adapter.maintain_history(self.scope,limit=1)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_history_windows').fetchone()[0],2)

    def test_second_verified_wallet_same_root_admitted_despite_first_gap(self):
        first=self.job()
        with patch.object(self.adapter,'fetch',return_value=self.mocked_fetch(b'',200)):
            self.adapter.run(first,self.scope)
        cursor=self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(first['id'],)).fetchone()[0]
        original_anchor=tuple(self.g.db.execute('SELECT * FROM frontier_anchors WHERE job=?',(self.parent,)).fetchone())
        block=dict(slot=SLOT,time=1736000000,transaction_nodes=1,decode_errors=[],transactions=[dict(
            signature=SIG,index=2,success=True,account_keys=[OTHER],transfers=[],activity=[],launches=[])])
        self.index.ingest(block,dict(car_sha256='f'*64,transaction_signatures_verified=True))
        second_parent=self.g.enqueue(ROOT,OTHER,SLOT,1736000000000,0,'local_forward',after_order=(2,1))
        self.enable();self.adapter.seed(self.scope)
        rows=self.g.db.execute("SELECT f.id,p.body FROM frontier f JOIN portal_jobs p ON p.job=f.id WHERE f.wallet=? AND f.source='portal_forward'",(OTHER,)).fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0]['body'])['parent_job'],second_parent)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(first['id'],)).fetchone()[0],cursor)
        self.assertEqual(tuple(self.g.db.execute('SELECT * FROM frontier_anchors WHERE job=?',(self.parent,)).fetchone()),original_anchor)

    def test_exact_old_quota_failure_resumes_once_without_rewriting_error(self):
        job=self.job();error=dict(error_type='ValueError',error='PORTAL_EVALUATION_IO_LIMIT',retryable=False)
        with self.g.db:
            self.g.db.execute("UPDATE frontier SET state='source_blocked' WHERE id=?",(job['id'],))
            self.g.set('source_error:'+job['id'],error)
        self.enable();self.adapter.maintain_history(self.scope)
        row=self.g.db.execute('SELECT state,cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()
        self.assertEqual(row['state'],'pending');self.assertEqual(row['cursor'],job['cursor'])
        self.assertEqual(self.g.state('source_error:'+job['id']),error)
        PortalHistory(self.g).maintain_history(self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_continuation_recoveries').fetchone()[0],1)

    def test_schema_or_identity_source_failure_is_not_quota_recovered(self):
        job=self.job()
        with self.g.db:
            self.g.db.execute("UPDATE frontier SET state='source_blocked' WHERE id=?",(job['id'],))
            self.g.set('source_error:'+job['id'],dict(error_type='ValueError',error='PORTAL_WRONG_WALLET'))
        self.enable();self.adapter.maintain_history(self.scope)
        self.assertEqual(self.g.db.execute('SELECT state FROM frontier WHERE id=?',(job['id'],)).fetchone()[0],'source_blocked')
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_continuation_recoveries').fetchone()[0],0)

    def test_inventory_scope_blocks_source_request_before_reservation(self):
        self.enable()
        with patch('economic_machine.pump_research.portal_history.urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(ValueError,'SLOT_BOUND'):
                self.adapter.fetch(query(WALLET,500000000,500000000),self.scope)
        opener.assert_not_called();self.assertEqual(self.reserved,[])

    def test_provider_floor_skips_futile_windows_with_explicit_gap(self):
        self.job();self.enable();self.metadata(SLOT-3);job=self.history_job()
        window=self.g.db.execute('SELECT * FROM portal_history_windows').fetchone()
        self.assertEqual(window['lower_slot'],SLOT-3)
        gap=json.loads(self.g.db.execute('SELECT body FROM portal_unresolved_gaps').fetchone()[0])
        self.assertEqual(gap['end_slot'],SLOT-4);self.assertFalse(gap['absence_proven'])
        with patch.object(self.adapter,'fetch',return_value=self.mocked_fetch(b'',200)):
            self.adapter.run(job,self.scope)
        self.adapter.maintain_history(self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_history_windows').fetchone()[0],1)

    def test_original_forward_below_provider_floor_keeps_cursor_and_does_not_fetch(self):
        job=self.job();self.enable();self.metadata(SLOT+3)
        before=self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0]
        with patch.object(self.adapter,'fetch') as fetch:
            value=self.adapter.run(job,self.scope)
        fetch.assert_not_called();self.assertTrue(value['no_request'])
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0],before)

    def test_metadata_hash_mismatch_is_not_accepted_as_provider_floor(self):
        path,_=self.metadata(0);path.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'SOURCE_IDENTITY'):self.adapter.dataset_floor()

    def test_missing_metadata_is_unknown_not_invented_floor(self):
        self.job();self.enable();job=self.history_job()
        meta=json.loads(self.g.db.execute('SELECT body FROM portal_jobs WHERE job=?',(job['id'],)).fetchone()[0])
        self.assertIsNone(meta['provider_start_metadata']);self.assertFalse(meta['provider_start_known'])

    def test_continuation_429_keeps_new_reservation_and_original_cursor(self):
        job=self.job();self.enable();before=job['cursor']
        error=urllib.error.HTTPError('https://portal.sqd.dev/',429,'limited',{'Retry-After':'120'},None)
        self.addCleanup(error.close)
        with patch('economic_machine.pump_research.portal_history.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(urllib.error.HTTPError):self.adapter.run(job,self.scope)
        row=self.g.db.execute('SELECT status,body FROM portal_continuation_io').fetchone()
        self.assertEqual(row['status'],'http_error');self.assertEqual(json.loads(row['body'])['outer_reservation'],1)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(job['id'],)).fetchone()[0],before)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_source_io').fetchone()[0],0)

    def test_success_receipt_namespace_cannot_overwrite_legacy_request(self):
        self.enable();body=query(WALLET,SLOT,SLOT);request_hash=sha(canonical(body).encode())
        old=self.adapter.folder/f'1-{request_hash[:16]}.receipt.json';old.write_bytes(b'old receipt')
        response=io.BytesIO(line());response.status=200
        with patch('economic_machine.pump_research.portal_history.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=response
            _,receipt=self.adapter.fetch(body,self.scope)
        self.assertEqual(old.read_bytes(),b'old receipt')
        self.assertEqual(receipt['source_ledger'],'portal_continuation_io')
        self.assertTrue((self.adapter.folder/f'continuation-{"d"*16}-1-{request_hash[:16]}.receipt.json').exists())


if __name__=='__main__':unittest.main()
