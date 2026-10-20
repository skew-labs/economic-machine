import json
import struct
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from economic_machine.pump_research.archive import Archive
from economic_machine.pump_research.raw_history import corroborate,ScopedArchive,ArchiveDeferred
from economic_machine.pump_research.archive_gate import ArchiveGate
from types import SimpleNamespace
import urllib.error
from economic_machine.pump_research.wallet_map import Graph,UncappedMuse,MODEL,select_jobs
from economic_machine.pump_research.store import encode
from economic_machine.pump_research.research_quality import event_key
import time

class RangeTests(unittest.TestCase):
    def reader(self, present):
        reader=Archive(None,None);self.requests=[]
        def request(url,start,end):
            self.requests.append((url,start,end))
            epoch=int(url.split('/')[-2]);first=epoch*432000+start//12
            return b''.join(struct.pack('<QI',s*10,99 if s in present else 0) for s in range(first,first+(end-start+1)//12))
        reader.request=request
        return reader
    def test_run_longer_than_four_absent_slots(self):
        r=self.reader({436161181});self.assertEqual(r.next_indexed_slot(436161164,436161988),436161181)
        self.assertEqual(len(self.requests),1)
    def test_epoch_boundary(self):
        boundary=1010*432000;r=self.reader({boundary+4})
        self.assertEqual(r.next_indexed_slot(boundary-4,boundary+8),boundary+4)
        self.assertEqual(len(self.requests),2)
    def test_absence_not_a_block(self):
        self.assertIsNone(self.reader(set()).next_indexed_slot(436161164,436161900))
    def test_bounds(self):
        with self.assertRaises(ValueError):self.reader(set()).next_indexed_slot(436161164,436165260)
    def test_short_response(self):
        r=Archive(None,None);r.request=lambda *a:b'\0'*11
        with self.assertRaisesRegex(ValueError,'TRUNCATED'):r.next_indexed_slot(436161164,436161164)

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name);out=base/'map';out.mkdir()
        self.reservations=[]
        scope=SimpleNamespace(guard=lambda:None,reserved=lambda:0,grant={'additional_io_bytes':12*1024**3},reserve=lambda n,p:self.reservations.append(n))
        self.reader=ScopedArchive(SimpleNamespace(out=out,base=base),scope,None,None,None)
    def test_adjacent_index_entries_share_one_request(self):
        url='https://files.old-faithful.net/1009/epoch-1009-slot-ranges.raw'
        with patch('economic_machine.pump_research.raw_history.get',return_value=b'x'*3072) as get:
            self.assertEqual(self.reader.request(url,12,23),b'x'*12)
            self.assertEqual(self.reader.request(url,24,35),b'x'*12)
        self.assertEqual(get.call_count,1);self.assertEqual(self.reservations,[3072])
    def test_cache_hit_bypasses_request_lock_and_provider_cooldown(self):
        url='https://files.old-faithful.net/1009/epoch-1009.car'
        with patch('economic_machine.pump_research.raw_history.get',return_value=b'x'*100):
            self.assertEqual(self.reader.request(url,0,99),b'x'*100)
        self.reader.wait=False
        with ArchiveGate(self.reader.folder,self.reader.scope.guard):
            (self.reader.folder/'network-state.json').write_text(json.dumps(dict(not_before=time.time()+120,last_status=429)))
            with patch('economic_machine.pump_research.raw_history.get') as get:
                self.assertEqual(self.reader.request(url,0,99),b'x'*100)
            get.assert_not_called()
        self.assertEqual(self.reservations,[100])
    def test_429_keeps_reservation_and_retry_after(self):
        err=urllib.error.HTTPError('https://files.old-faithful.net/',429,'rate limit',{'Retry-After':'120'},None)
        with patch('economic_machine.pump_research.raw_history.get',side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):self.reader.request('https://files.old-faithful.net/1009/epoch-1009.car',0,99)
        state=json.loads((self.reader.folder/'network-state.json').read_bytes())
        self.assertGreater(state['not_before'],time.time()+119)
        self.assertEqual(self.reservations,[100]);self.assertTrue(state['failed_reservation_retained'])
    def test_guard_work_crossing_retry_time_never_sleeps_negative(self):
        (self.reader.folder/'network-state.json').write_text(json.dumps(dict(not_before=10)))
        now=[0];self.reader.scope.guard=lambda:now.__setitem__(0,11)
        def sleep(delay):self.assertGreaterEqual(delay,0)
        with patch('economic_machine.pump_research.raw_history.time.time',side_effect=lambda:now[0]),patch('economic_machine.pump_research.raw_history.time.sleep',side_effect=sleep),patch('economic_machine.pump_research.raw_history.get',return_value=b'x'*100):
            self.assertEqual(len(self.reader.request('https://files.old-faithful.net/1009/epoch-1009.car',0,99)),100)
    def test_nonblocking_cooldown_preserves_network_state_and_reservations(self):
        self.reader.wait=False
        state=json.dumps(dict(not_before=time.time()+120,rate_limit_attempts=3))
        (self.reader.folder/'network-state.json').write_text(state)
        with patch('economic_machine.pump_research.raw_history.get') as get,patch('economic_machine.pump_research.raw_history.time.sleep') as sleep:
            with self.assertRaises(ArchiveDeferred) as error:self.reader.request('https://files.old-faithful.net/1009/epoch-1009.car',0,99)
        self.assertGreater(error.exception.retry_at_ms,time.time()*1000+119000)
        self.assertEqual(self.reservations,[]);get.assert_not_called();sleep.assert_not_called()
        self.assertEqual((self.reader.folder/'network-state.json').read_text(),state)

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.sig=encode(bytes([7])*64)
        self.edge=dict(source='a',target='b',body=json.dumps(dict(signature=self.sig,amount='1.000000001',symbol='SOL')))
        self.tx=dict(signature=self.sig,index=4,success=True,transfers=[dict(source='a',destination='b',mint='SOL',amount='1000000001',ix=3)])
        self.block=dict(transactions=[self.tx],decode_errors=[])
    def test_exact_lamports(self):
        out=corroborate(self.edge,self.block)
        self.assertEqual(out['state'],'raw_transfer_corroborated');self.assertFalse(out['independent_consensus_verified'])
    def test_failed_transaction_not_transfer(self):
        self.tx['success']=False;self.assertEqual(corroborate(self.edge,self.block)['state'],'transaction_failed')
    def test_amount_and_destination_must_match(self):
        self.tx['transfers'][0]['amount']='1000000000'
        self.assertEqual(corroborate(self.edge,self.block)['state'],'transfer_fields_mismatch')
        self.tx['transfers'][0]['amount']='1000000001';self.edge['target']='c'
        self.assertEqual(corroborate(self.edge,self.block)['state'],'transfer_fields_mismatch')
    def test_missing_signature_not_proof_of_no_transfer(self):
        self.block['transactions']=[]
        self.assertEqual(corroborate(self.edge,self.block)['state'],'signature_not_decoded')
    def test_partial_lamport_rejected(self):
        self.edge['body']=json.dumps(dict(signature=self.sig,amount='1.0000000001',symbol='SOL'))
        self.assertEqual(corroborate(self.edge,self.block)['state'],'invalid_dataset_amount')
    def test_invalid_signature_is_unverified(self):
        self.edge['body']=json.dumps(dict(signature='0notbase58',amount='1',symbol='SOL'))
        self.assertEqual(corroborate(self.edge,self.block)['state'],'invalid_dataset_signature')
    def test_oversized_amount_is_not_materialized_as_integer(self):
        self.edge['body']=json.dumps(dict(signature=self.sig,amount='1e999999999',symbol='SOL'))
        self.assertEqual(corroborate(self.edge,self.block)['state'],'invalid_dataset_amount')
    def test_archive_and_index_same_instruction_are_one_observation(self):
        e=dict(kind='raw_archive_transfer',evidence=dict(signature=self.sig,ix=3),slot=300000000,source='a',target='b')
        self.assertEqual(event_key(e),event_key(dict(e,kind='indexed_transfer')))

class IntegrationTests(unittest.TestCase):
    def test_launch_recovery_yields_cooldown_without_advancing_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'evidence').mkdir();g=Graph(base)
            try:
                root=encode(bytes([1])*32)
                with g.db:
                    jid=g.enqueue(root,root,300000000,1706000000000,0,'recover_launch')
                    cursor=json.dumps(dict(start=300000000,end=300000003,next=300000000))
                    g.db.execute('UPDATE frontier SET cursor=? WHERE id=?',(cursor,jid))
                job=dict(g.db.execute('SELECT * FROM frontier WHERE id=?',(jid,)).fetchone())
                state=json.dumps(dict(not_before=time.time()+120,rate_limit_attempts=3))
                folder=g.out/'archive-ranges';folder.mkdir(exist_ok=True)
                (folder/'network-state.json').write_text(state)
                scope=SimpleNamespace(guard=lambda *a,**kw:None,reserved=lambda:0,
                    grant={'additional_io_bytes':12*1024**3})
                with patch('economic_machine.pump_research.cli.proto',return_value=(None,None)),patch('economic_machine.pump_research.raw_history.get') as get,patch('economic_machine.pump_research.raw_history.time.sleep') as sleep:
                    with self.assertRaises(ArchiveDeferred):g.recover_launch(job,scope)
                self.assertEqual(g.db.execute('SELECT cursor FROM frontier WHERE id=?',(jid,)).fetchone()[0],cursor)
                self.assertEqual((folder/'network-state.json').read_text(),state)
                get.assert_not_called();sleep.assert_not_called()
            finally:g.close()

    def test_source_record_preserved_and_seed_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'evidence').mkdir();g=Graph(base)
            try:
                w=[encode(bytes([i])*32) for i in (1,2,3)]
                with g.db:
                    g.db.execute('INSERT INTO roots VALUES(?,?)',(w[0],'{}'))
                    edge=g.edge(*w,'funding_record',300000000,1736000000000,signature=encode(bytes([7])*64),amount='1',symbol='SOL')
                before=tuple(g.db.execute('SELECT * FROM edges WHERE id=?',(edge,)).fetchone())
                g.raw_history.seed(lambda:None);g.raw_history.seed(lambda:None)
                self.assertEqual(g.db.execute('SELECT count(*) FROM raw_history_jobs').fetchone()[0],1)
                self.assertEqual(tuple(g.db.execute('SELECT * FROM edges WHERE id=?',(edge,)).fetchone()),before)
            finally:g.close()
    def test_provider_unknown_cost_stays_reserved(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'evidence').mkdir();g=Graph(base)
            b=UncappedMuse(base/'model.sqlite3',dict(authorization='owner_unlimited_muse_top10_forward_wallet_research',maximum_usd=None,max_calls=None,model=MODEL,trade_authority=False,deadline_ms=time.time_ns()//1000000+100000))
            try:
                w=[encode(bytes([i])*32) for i in (1,2)]
                with g.db:
                    g.db.execute('INSERT INTO roots VALUES(?,?)',(w[0],'{}'));g.enqueue(w[0],w[1],300000000,1736000000000,0)
                with patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=ValueError('provider outcome unknown')):
                    with self.assertRaisesRegex(ValueError,'MAP_MODEL_PROVIDER_TRANSIENT'):select_jobs(g,b,'unused',None)
                self.assertEqual(b.totals()['unconfirmed_cost_reservations'],1)
                self.assertEqual(b.totals()['responses_received'],0)
            finally:b.db.close();g.close()

if __name__=='__main__':unittest.main()
