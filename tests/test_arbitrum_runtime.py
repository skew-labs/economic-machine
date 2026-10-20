import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from economic_machine.arbitrum_research.authority import activate,authority
from economic_machine.arbitrum_research.runtime import Runtime,investigation_jobs,apply_limits,working_cache_bytes
from economic_machine.arbitrum_research.source import parse_page
from economic_machine.arbitrum_research.storage import Storage
from economic_machine.arbitrum_research.governor import conservative_proposal
from economic_machine.arbitrum_research.muse import validate_summary


def h(n):return '0x'+f'{n:064x}'
def a(n):return '0x'+f'{n:040x}'
def page(start,end):
    blocks=[dict(header=dict(number=i,hash=h(i+1),parentHash=h(i),timestamp=i),
        transactions=[dict(transactionIndex=0,hash=h(10000+i),status=1,
            **{'from':a(1),'to':a(2),'value':'0x7'})],logs=[],traces=[]) for i in range(start,end+1)]
    raw=('\n'.join(json.dumps(b) for b in blocks)+'\n').encode()
    return parse_page(raw,start,end)


def measured(*_):
    return dict(observed_ms=time.time_ns()//1_000_000,logical_cpus=16,cpu_busy_pct=30,
        io_pressure_pct=5,mem_available_bytes=32*1024**3,mem_total_bytes=48*1024**3,
        free_disk_bytes=256*1024**3,protected_free_bytes=128*1024**3,
        arb_current_bytes=0,monthly_remaining_krw=700000,source_backoff_seconds=0,backlog_blocks=100)


class FakeSpend:
    def totals(self):return dict(remaining_krw=700000,monthly_limit_krw=1000000)


class FakeCloud:
    blobs={}
    def __init__(self,*_):self.spend=FakeSpend()
    def upload(self,path):
        raw=Path(path).read_bytes();sha=hashlib.sha256(raw).hexdigest();self.blobs[sha]=raw
        return dict(sha256=sha,bytes=len(raw))
    def cached(self,sha,size):
        raw=self.blobs[sha]
        if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('bad cloud')
        return io.BytesIO(raw)
    def close(self):pass


class FakeSource:
    def head(self):return 1000
    def fetch(self,start,end):return page(start,end)


class FakeFleet:
    def __init__(self,*_):pass
    def manage(self,m):
        p=conservative_proposal(m);p['download_batch_blocks']=4
        return dict(proposal=p,status='valid')
    def investigate(self,jobs,max_workers=30):
        return dict(batch_id='test',results=[],dispatched=len(jobs),configured_workers=max_workers)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        (self.base/'evidence').mkdir();FakeCloud.blobs={}
        self.patches=[patch('economic_machine.arbitrum_research.runtime.authority',return_value=({},'a'*64)),
            patch('economic_machine.arbitrum_research.source.PublicSource',FakeSource),
            patch('economic_machine.arbitrum_research.muse.MuseFleet',FakeFleet),
            patch('economic_machine.arbitrum_research.storage.Cloud',FakeCloud),
            patch('economic_machine.arbitrum_research.runtime.metrics',measured),
            patch('economic_machine.arbitrum_research.runtime.shutil.disk_usage',return_value=shutil._ntuple_diskusage(500*1024**3,100*1024**3,400*1024**3))]
        for p in self.patches:p.start()
        self.r=Runtime(self.base,self.base/'shared',self.base/'unused')
    def tearDown(self):
        if self.r:self.r.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def test_genesis_then_contiguous_followup_archived_before_cursor(self):
        first=self.r.one();second=self.r.one()
        self.assertEqual((first['first_block'],second['last_block']), (0,7))
        self.assertEqual(self.r.state['next_block'],8)
        self.assertEqual(self.r.graph.snapshot()['counts']['edges'],8)
        self.assertTrue(all(x[0]=='indexed' for x in self.r.db.execute('SELECT state FROM pages')))
        measures=[json.loads(r[0]) for r in self.r.db.execute('SELECT body FROM pipeline_metrics')]
        self.assertEqual(len(measures),2)
        self.assertTrue(all(r['indexed_after_blob_verification'] for r in measures))
        self.assertEqual([r['first_block'] for r in measures],[0,4])

    def test_restart_preserves_next_block_and_prior_graph(self):
        self.r.one();self.r.close();self.r=Runtime(self.base,self.base/'shared',self.base/'unused')
        p=self.r.one();self.assertEqual(p['first_block'],4)
        self.assertEqual(self.r.graph.snapshot()['counts']['blocks'],8)

    def test_restart_reapplies_kernel_limits_even_with_same_journal_policy(self):
        self.r.one();self.r.close()
        self.r=Runtime(self.base,self.base/'shared',self.base/'unused',resource_apply=True)
        with patch('economic_machine.arbitrum_research.runtime.apply_limits',return_value={'applied':True}) as apply:
            self.r.reclamp();self.r.reclamp()
        apply.assert_called_once()
        self.assertFalse(self.r.resource_reapply_pending)

    def test_graph_committed_before_cursor_crash_is_idempotent(self):
        original=self.r.graph.ingest
        def committed_then_interrupted(*args):
            original(*args);raise RuntimeError('simulated interruption')
        with patch.object(self.r.graph,'ingest',side_effect=committed_then_interrupted):
            with self.assertRaises(RuntimeError):self.r.one()
        self.assertEqual(self.r.state['next_block'],0)
        self.r.close();self.r=Runtime(self.base,self.base/'shared',self.base/'unused')
        self.r.one();self.assertEqual(self.r.state['next_block'],4)
        self.assertEqual(self.r.graph.snapshot()['counts']['edges'],4)

    def test_cloud_failure_never_advances(self):
        with patch.object(self.r.storage.cloud,'upload',side_effect=OSError('cloud failed')):
            with self.assertRaises(OSError):self.r.one()
            with self.assertRaises(OSError):self.r.one()
        self.assertEqual(self.r.state['next_block'],0)
        self.assertEqual(self.r.graph.snapshot()['counts']['blocks'],0)
        self.assertTrue(list((self.base/'staging').glob('*.gz')))
        self.assertEqual(len(list((self.base/'staging').glob('*.gz'))),1)

    def test_pending_replay_precedes_cache_rotation(self):
        original=self.r.graph.ingest
        def interrupted(*args):original(*args);raise RuntimeError('interrupt')
        with patch.object(self.r.graph,'ingest',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.r.one()
        with patch.object(self.r,'guard',side_effect=AssertionError('must not seal before replay')):
            self.r.one()
        self.assertEqual(self.r.state['next_block'],4)

    def test_source_byte_limit_survives_native_reclamp(self):
        self.r.one();self.r.state['source_batch_cap']=2
        self.r.reclamp();self.assertEqual(self.r.state['policy']['download_batch_blocks'],2)

    def test_shard_rotation_carries_tip_and_verified_blob(self):
        self.r.one();old=self.r.graph_path
        self.r.seal();self.assertFalse(old.exists())
        self.assertEqual(self.r.state['shard_start'],4)
        self.assertEqual(self.r.graph.snapshot()['tip_hash'],h(4))
        self.r.one();self.assertEqual(self.r.graph.snapshot()['covered_last_block'],7)
        row=self.r.db.execute('SELECT body FROM shards').fetchone()[0]
        archived=json.loads(row);manifest=archived['manifest']
        raw=b''.join(self.r.storage.read(p) for p in manifest['parts'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['sha256'])
        restore=self.base/'restore.sqlite3';restore.write_bytes(raw)
        with sqlite3.connect(restore) as db:self.assertEqual(db.execute('SELECT count(*) FROM blocks').fetchone()[0],4)

    def test_sealed_before_switch_crash_recovers_new_shard(self):
        self.r.one();self.r.seal()
        self.r.state.update(shard_start=0,previous_shard_hash=None);self.r.save()
        self.r.close();self.r=Runtime(self.base,self.base/'shared',self.base/'unused')
        self.assertEqual(self.r.state['shard_start'],4)
        self.r.one();self.assertEqual(self.r.state['next_block'],8)

    def test_rejected_manager_uses_explicit_native_fallback(self):
        with patch.object(self.r.fleet,'manage',return_value=dict(proposal=None,status='rejected')):
            self.r.one()
        self.assertFalse(self.r.state['policy']['manager_response_applied'])

    def test_thirty_jobs_are_distinct_and_source_bound(self):
        p=self.r.one();jobs=investigation_jobs(p,self.r.graph.snapshot())
        self.assertEqual(len(jobs),30)
        for job in jobs:validate_summary(job)
        self.assertEqual(len({j['records']['question'] for j in jobs}),30)
        self.assertEqual({j['source_ids'][0] for j in jobs},{p['sha256']})

    def test_budget_exhaustion_stops_before_source(self):
        with patch.object(self.r.storage.cloud.spend,'totals',return_value=dict(remaining_krw=0)):
            with patch.object(self.r.source,'fetch') as f:
                with self.assertRaisesRegex(ValueError,'MONTHLY'):self.r.one()
                f.assert_not_called()

    def test_retained_evidence_is_not_mutable_working_cache(self):
        path=self.base/'evidence'/'retained-source'
        path.write_bytes(b'x'*8192)
        before=working_cache_bytes(self.base)
        self.r.state['policy']={'cache_limit_bytes':before+1024}
        self.r.guard(allow_seal=False)
        self.assertTrue(path.exists())
        (self.base/'staging'/'failed-upload').write_bytes(b'y'*2048)
        with self.assertRaisesRegex(ValueError,'DYNAMIC_CACHE'):
            self.r.guard(allow_seal=False)
        self.assertTrue(path.exists())

    def test_global_disk_floor_applies_despite_small_working_cache(self):
        self.r.state['policy']={'cache_limit_bytes':2**40}
        with patch('economic_machine.arbitrum_research.runtime.shutil.disk_usage',
                   return_value=shutil._ntuple_diskusage(500*1024**3,400*1024**3,100*1024**3)):
            with self.assertRaisesRegex(ValueError,'HOST_FREE_RESERVE'):
                self.r.guard()

    def test_run_status_never_claims_whole_history_or_finality(self):
        self.r.one();status=self.r.publish('INDEXING')
        self.assertFalse(status['whole_history_complete'])
        self.assertFalse(status['independent_consensus_verified'])
        self.assertFalse(status['private_exchange_ledger_available'])

    def test_missing_trace_rows_are_durable_per_transaction_gaps(self):
        self.r.one()
        self.assertEqual(self.r.db.execute('SELECT count(*) FROM coverage_gaps').fetchone()[0],4)

    def test_corroboration_mismatch_halts_after_restart(self):
        p=self.r.one()
        result=dict(raw=b'{"outcome":"MISMATCH"}',outcome='MISMATCH',at_ms=123,
            records=[],block_checks=[],transaction_checks=[],original_gaps=[])
        with patch('economic_machine.arbitrum_research.audit.audit_page',return_value=result):
            with self.assertRaisesRegex(ValueError,'MISMATCH'):self.r.audit(p)
        self.r.close();self.r=Runtime(self.base,self.base/'shared',self.base/'unused')
        with patch.object(self.r.source,'fetch') as fetch:
            with self.assertRaisesRegex(ValueError,'MISMATCH'):self.r.one()
            fetch.assert_not_called()


class StorageTests(unittest.TestCase):
    def test_corrupt_cloud_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as d,patch('economic_machine.arbitrum_research.storage.Cloud',FakeCloud):
            FakeCloud.blobs={};s=Storage(d,d);p=s.put(b'public raw source','test')
            self.assertEqual(s.read(p),b'public raw source')
            FakeCloud.blobs[p['cloud']['sha256']]=b'corrupt'
            with self.assertRaises(ValueError):s.read(p)
            s.close()
    def test_authority_does_not_rewrite_existing_grant(self):
        with tempfile.TemporaryDirectory() as d,patch('economic_machine.arbitrum_research.authority.shared_authority',return_value=({},'b'*64)):
            a1=activate(d,d);a2=activate(d,d);self.assertEqual(a1,a2)
            p=Path(d)/'evidence/grant.json';v=json.loads(p.read_bytes());v['investigators']=31;p.write_text(json.dumps(v))
            with self.assertRaises(ValueError):authority(d,d)
    def test_resource_apply_rejects_outside_fixed_command_surface(self):
        with self.assertRaises(ValueError):apply_limits(dict(cpu_quota_percent='800%; echo bad',memory_max_bytes=1024**3,io_weight=10),True)


if __name__=='__main__':unittest.main()
