import hashlib
import tempfile
import unittest
import zlib
from pathlib import Path
from economic_machine.pump_research import object_store as cas
from economic_machine.pump_research.compact import objects,transfers
from economic_machine.pump_research.store import Store
from economic_machine.pump_research.shadow import funders_before,schema
from test_pump_strategy import key,event,pack
from types import SimpleNamespace as NS
from economic_machine.pump_research.events import evidence_events,EVENT_CPI,scoped_logs
from economic_machine.pump_research.store import PUMP

class EventRecoveryTests(unittest.TestCase):
    def fixture(self,parent=PUMP,height=2,failed=False):
        ix=NS(program_id_index=0,data=EVENT_CPI+pack(event()),stack_height=height)
        meta=NS(inner_instructions=[NS(index=0,instructions=[ix])],log_messages=[f'Program {PUMP} invoke [1]'],
                fee=5000,HasField=lambda _:failed)
        activity=[dict(ix=0,mint=key(1),wallet=key(2),action='buy')]
        return meta,activity,[(1,[],b'')],[PUMP,parent]
    def test_direct_self_cpi_recovers_truncated_log(self):
        out,errors=evidence_events(*self.fixture());self.assertEqual(len(out),1)
        self.assertEqual(out[0]['event_evidence'],'direct_pump_self_cpi')
        self.assertIn('INCOMPLETE_INVOCATION_STACK',errors)
    def test_router_parent_not_promoted_from_truncated_logs(self):
        self.assertEqual(evidence_events(*self.fixture(parent=key(9)))[0],[])
    def test_wrong_depth_not_committed_evidence(self):
        for h in (0,1,3):self.assertEqual(evidence_events(*self.fixture(height=h))[0],[])
    def test_failed_transaction_never_recovered(self):
        self.assertEqual(evidence_events(*self.fixture(failed=True))[0],[])
    def test_event_mismatch_rejected(self):
        m,a,o,k=self.fixture();a[0]['wallet']=key(3)
        out,errors=evidence_events(m,a,o,k);self.assertFalse(out);self.assertIn('CPI_EVENT_INSTRUCTION_MISMATCH',errors)
    def test_wrong_program_or_tag_ignored(self):
        m,a,o,k=self.fixture();m.inner_instructions[0].instructions[0].program_id_index=1;k[1]=key(9)
        self.assertEqual(evidence_events(m,a,o,k)[0],[])
        m,a,o,k=self.fixture();m.inner_instructions[0].instructions[0].data=b'fake_tag'+pack(event())
        self.assertEqual(evidence_events(m,a,o,k)[0],[])
    def test_duplicate_cpi_trade_is_ambiguous(self):
        args=self.fixture();args[0].inner_instructions[0].instructions*=2
        self.assertIn('CPI_TRADE_CARDINALITY',evidence_events(*args)[1])
    def test_log_and_cpi_deduplicate(self):
        import base64
        m,a,o,k=self.fixture();m.log_messages=[f'Program {PUMP} invoke [1]','Program data: '+base64.b64encode(pack(event())).decode(),f'Program {PUMP} success']
        out,errors=evidence_events(m,a,o,k);self.assertEqual(len(out),1);self.assertFalse(errors)
        self.assertEqual(out[0]['event_evidence'],'logs_and_direct_pump_self_cpi')
        e=event();e['sol_amount']+=1;m.inner_instructions[0].instructions[0].data=EVENT_CPI+pack(e)
        self.assertIn('LOG_CPI_EVENT_CONFLICT',evidence_events(m,a,o,k)[1])
    def test_caught_failed_invocation_logs_not_committed(self):
        import base64
        m,a,_,_=self.fixture();m.log_messages=[f'Program {key(9)} invoke [1]',f'Program {PUMP} invoke [2]',
            'Program data: '+base64.b64encode(pack(event())).decode(),f'Program {PUMP} failed: custom program error: 1',f'Program {key(9)} success']
        self.assertEqual(scoped_logs(m,a),([],['FAILED_INNER_INVOCATION']))

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name,reserve=0)
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def legacy_object(self,raw):
        h=hashlib.sha256(raw).hexdigest();(self.s.root/'objects'/h).write_bytes(raw)
        with self.s.db:
            self.s.db.execute('INSERT INTO objects(hash,bytes) VALUES(?,?)',(h,len(raw)))
            self.s.set_state('object_bytes',self.s.state('object_bytes')+len(raw))
        return h
    def observation(self,signature,slot,edges,observed=90):
        with self.s.db:
            ev=self.s.evidence('test',signature,signature.encode())
            self.s.db.execute('UPDATE evidence SET observed=? WHERE id=?',(observed,ev))
            self.s.ingest(dict(signature=signature,slot=slot,success=True,activity=[],launches=[],transfers=edges),ev,'test','processed_unconfirmed')
        return self.s.db.execute('SELECT id FROM observations WHERE evidence=?',(ev,)).fetchone()[0]
    def edge(self,n,src=None,dst=None):
        return dict(ix=n,source=src or key(1),destination=dst or key(2),mint='SOL',amount='10000000',kind='system_transfer',authority=src or key(1))
    def test_compressed_roundtrip_deduplicates_raw_identity(self):
        raw=b'public evidence '*1000
        with self.s.db:a=self.s.evidence('a','1',raw);b=self.s.evidence('b','2',raw)
        h=hashlib.sha256(raw).hexdigest();self.assertNotEqual(a,b)
        self.assertEqual(self.s.read_object(h),raw)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM objects').fetchone()[0],1)
        self.assertLess(self.s.state('object_bytes'),len(raw))
    def test_legacy_compaction_exact_and_restart(self):
        raw=b'old bytes'*1000;h=self.legacy_object(raw)
        result=objects(self.s);self.assertEqual(result['exact_hash_verified'],1)
        self.assertEqual(self.s.read_object(h),raw);self.assertFalse((self.s.root/'objects'/h).exists())
        self.assertEqual(objects(self.s)['objects'],0)
    def test_interrupted_compaction_before_metadata_commit(self):
        raw=b'recover'*1000;h=self.legacy_object(raw);data,codec=cas.encode(raw)
        cas.write(self.s.root,h,data,codec)
        objects(self.s);self.assertEqual(self.s.read_object(h),raw)
        self.assertEqual(self.s.state('object_bytes'),len(data))
    def test_compressed_corruption_never_falls_back(self):
        raw=b'valid'*1000;h=self.legacy_object(raw)
        cas.paths(self.s.root,h)[1].write_bytes(zlib.compress(b'forged'))
        with self.assertRaisesRegex(ValueError,'HASH'):objects(self.s)
        self.assertTrue(cas.paths(self.s.root,h)[0].exists())
    def test_compressed_trailing_or_truncated_rejected(self):
        for data in (zlib.compress(b'x')+b'tail',zlib.compress(b'x')[:-1]):
            with self.assertRaisesRegex(ValueError,'BOUND'):cas.unpack(data)
    def test_decompression_bound(self):
        from unittest.mock import patch
        with patch.object(cas,'MAX_RAW',100):
            with self.assertRaisesRegex(ValueError,'BOUND'):cas.unpack(zlib.compress(b'x'*101))
    def test_orphan_compressed_copy_counted(self):
        self.s.evidence('test','x',b'x'*1000);self.s.db.commit()
        stray=self.s.root/'objects'/('0'*64+'.z');stray.write_bytes(b'y'*100)
        self.s.db.close();self.s=Store(self.tmp.name,reserve=0)
        self.assertEqual(self.s.orphan_bytes,100)
    def test_transfer_compaction_exact_queries_and_new_ingest(self):
        self.observation('one',80,[self.edge(0),self.edge(1,key(3),key(2))])
        before=[tuple(x) for x in self.s.db.execute('SELECT * FROM transfers ORDER BY observation,ix')]
        result=transfers(self.s);schema(self.s)
        self.assertEqual(result['exact_rows_matched'],2)
        self.assertEqual(before,[tuple(x) for x in self.s.db.execute('SELECT * FROM transfers ORDER BY observation,ix')])
        self.s.db.close();self.s=Store(self.tmp.name,reserve=0)
        self.observation('two',81,[self.edge(0)])
        self.assertEqual(len(self.s.trace(key(2))['edges']),3)
        self.assertEqual(transfers(self.s),dict(already_compacted=True))
    def test_fast_status_identical_on_normalized_schema(self):
        from economic_machine.pump_research.reporting import summary
        self.observation('one',80,[self.edge(0),self.edge(1)]);transfers(self.s)
        old=self.s.summary();new=summary(self.s);old.pop('at');new.pop('at')
        self.assertEqual(old,new)
    def test_target_funding_ignores_unrelated_edge_overflow(self):
        self.observation('many',80,[self.edge(i,key(8),key(9)) for i in range(5001)]+[self.edge(5001)])
        links,truncated=funders_before(self.s,dict(slot=100,observed=100),[key(2)])
        self.assertEqual(links,{key(2):key(1)});self.assertFalse(truncated)
    def test_funding_future_late_dead_and_dust_excluded(self):
        self.observation('good',70,[self.edge(0)])
        self.observation('future',100,[self.edge(0,key(3))])
        self.observation('late',80,[self.edge(0,key(4))],101)
        oid=self.observation('dead',81,[self.edge(0,key(5))])
        with self.s.db:self.s.db.execute("UPDATE observations SET finality='dead_branch' WHERE id=?",(oid,))
        e=self.edge(0,key(6));e['amount']='9999999';self.observation('dust',82,[e])
        self.assertEqual(funders_before(self.s,dict(slot=100,observed=100),[key(2)]),({key(2):key(1)},False))
    def test_high_fanout_not_labelled_common_owner(self):
        self.observation('hub',70,[self.edge(i,key(1),key(i+2)) for i in range(33)])
        self.assertEqual(funders_before(self.s,dict(slot=100,observed=100),[key(2)]),({},False))
    def test_target_edge_limit_still_fails_closed(self):
        self.observation('many',80,[self.edge(i) for i in range(5001)])
        self.assertEqual(funders_before(self.s,dict(slot=100,observed=100),[key(2)]),({},True))
    def test_expired_scope_cannot_resume_archive(self):
        from unittest.mock import patch
        from economic_machine.pump_research.cli import run_archive_job
        (self.s.root/'research-model-scope.json').write_text('{"deadline_ms":1}')
        with patch('economic_machine.pump_research.cli.Archive') as archive:
            self.assertEqual(run_archive_job(self.s,None,100),dict(state='RESEARCH_SCOPE_ENDED'))
            archive.assert_not_called()
    def test_address_cache_exact_and_bounded(self):
        from economic_machine.pump_research.store import encode,decode,_encode,_decode,_encode_key,_decode_key
        _encode_key.cache_clear();_decode_key.cache_clear()
        for n in range(100):
            raw=n.to_bytes(32,'big');self.assertEqual(encode(raw),_encode(raw))
            self.assertEqual(decode(encode(raw)),_decode(encode(raw)))
        for _ in range(3):self.assertEqual(decode(encode(bytes(32))),bytes(32))
        self.assertGreater(_encode_key.cache_info().hits,0);self.assertGreater(_decode_key.cache_info().hits,0)
        self.assertEqual(_encode_key.cache_info().maxsize,32768);self.assertEqual(_decode_key.cache_info().maxsize,32768)
    def test_signatures_bypass_address_cache(self):
        from economic_machine.pump_research.store import encode,_encode_key
        _encode_key.cache_clear();encode(b'x'*64);encode(b'x'*64)
        self.assertEqual(_encode_key.cache_info().currsize,0)
    def test_invalid_cached_address_remains_invalid(self):
        from economic_machine.pump_research.store import decode
        for _ in range(2):
            with self.assertRaises(ValueError):decode('0'*32)

if __name__=='__main__':unittest.main()
