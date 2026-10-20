import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'products/arbitrum_research'))
import tokyo_worker as worker
import parallel_history as controller
from economic_machine.arbitrum_research.parallel import SourceGate,CoordinatedSource,load_plan
from economic_machine.arbitrum_research.source import SourceBackoff,parse_page


def hash_(n):return '0x'+f'{n:064x}'
def page(start,end):
    blocks=[dict(header=dict(number=i,hash=hash_(i+1),parentHash=hash_(i),timestamp=i),
        transactions=[dict(hash=hash_(1000000000+i),transactionIndex=0,status=1,
            **{'from':'0x'+'1'*40,'to':'0x'+'2'*40,'value':'0x5'})],logs=[],traces=[]) for i in range(start,end+1)]
    return parse_page(('\n'.join(json.dumps(b) for b in blocks)+'\n').encode(),start,end)


class ParallelTests(unittest.TestCase):
    def test_recovery_single_slot_cannot_starve_waiting_genesis_writer(self):
        with tempfile.TemporaryDirectory() as d:
            now=[1000.];a=SourceGate(Path(d)/'gate',lambda:now[0]);b=SourceGate(Path(d)/'gate',lambda:now[0])
            t=a.acquire();a.release(t,10);now[0]=1011
            remote=a.acquire(owner='remote')
            with self.assertRaises(SourceBackoff):b.acquire()
            a.release(remote)
            with self.assertRaises(SourceBackoff):a.acquire(owner='remote')
            primary=b.acquire();b.release(primary)
            remote=a.acquire(owner='remote');a.release(remote);a.close();b.close()
    def test_abandoned_primary_priority_expires_without_extending_provider_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            now=[1000.];a=SourceGate(Path(d)/'gate',lambda:now[0])
            t=a.acquire();a.release(t,10);now[0]=1011
            remote=a.acquire(owner='remote')
            with self.assertRaises(SourceBackoff):a.acquire()
            a.release(remote);now[0]=1017
            remote=a.acquire(owner='remote');a.release(remote);a.close()
    def test_remote_lanes_leave_one_slot_for_primary_writer(self):
        with tempfile.TemporaryDirectory() as d:
            gate=SourceGate(Path(d)/'gate.db')
            remote=gate.acquire(owner='remote')
            with self.assertRaises(SourceBackoff):gate.acquire(owner='remote')
            primary=gate.acquire()
            gate.release(remote);gate.release(primary);gate.close()
    def test_source_capacity_shared_between_connections(self):
        with tempfile.TemporaryDirectory() as d:
            a=SourceGate(Path(d)/'gate.db');b=SourceGate(Path(d)/'gate.db')
            x=a.acquire();y=b.acquire()
            with self.assertRaises(SourceBackoff):a.acquire()
            a.release(x);z=b.acquire();b.release(y);b.release(z);a.close();b.close()

    def test_retry_after_shared_and_recovery_concurrency_lowered(self):
        with tempfile.TemporaryDirectory() as d:
            now=[1000.0];clock=lambda:now[0]
            a=SourceGate(Path(d)/'g',clock);b=SourceGate(Path(d)/'g',clock)
            token=a.acquire();a.release(token,90)
            with self.assertRaises(SourceBackoff) as error:b.acquire()
            self.assertEqual(error.exception.retry_after_seconds,90)
            now[0]=1091;t=b.acquire()
            with self.assertRaises(SourceBackoff):a.acquire()
            b.release(t);a.close();b.close()

    def test_provider_backoff_from_primary_is_visible_to_remote(self):
        class BadSource:
            def fetch(self,*_):raise SourceBackoff(80,429)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'g';source=CoordinatedSource(BadSource(),p)
            with self.assertRaises(SourceBackoff):source.fetch(0,1)
            gate=SourceGate(p)
            with self.assertRaises(SourceBackoff):gate.acquire()
            gate.close()

    def test_expired_lease_releases_capacity(self):
        with tempfile.TemporaryDirectory() as d:
            now=[0];gate=SourceGate(Path(d)/'g',lambda:now[0]);gate.acquire();gate.acquire()
            now[0]=601;gate.acquire();gate.close()

    def test_worker_request_rejects_paths_and_outside_partition(self):
        for value in ({'id':'../x','start':22207818,'end':22207820},
            {'id':'a'*32,'start':0,'end':1},{'id':'a'*32,'start':True,'end':22207820},
            {'id':'a'*32,'start':22207818,'end':22207820,'command':'ls'}):
            with self.assertRaises(ValueError):worker.request(value)

    def test_package_hash_bounds_and_trailing_gzip_rejected(self):
        raw=b'hello';packed=gzip.compress(raw)
        part=dict(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest(),
            raw_bytes=len(raw),raw_sha256=hashlib.sha256(raw).hexdigest())
        self.assertEqual(controller.unpack(packed,part,5),raw)
        with self.assertRaises(ValueError):controller.unpack(packed,part,4)
        extra=packed+gzip.compress(b'extra');part.update(bytes=len(extra),sha256=hashlib.sha256(extra).hexdigest())
        with self.assertRaises(ValueError):controller.unpack(extra,part,5)

    def test_truncated_transport_never_accepted(self):
        with self.assertRaises(ValueError):controller.read_exact(io.BytesIO(b'hi'),3)

    def test_remote_graph_rechecks_against_exact_source(self):
        with tempfile.TemporaryDirectory() as d:
            job=dict(id='a'*32,start=22207818,end=22207820)
            class Source:
                def fetch(self,start,end):return page(start,end)
            with patch.object(worker.shutil,'disk_usage',return_value=worker.shutil._ntuple_diskusage(30*1024**3,1,29*1024**3)):
                header=worker.prepare(job,Path(d),Source())
                self.assertEqual(worker.prepare(job,Path(d),Source()),header)
            folder=Path(d)/job['id'];raw=(folder/'source.ndjson').read_bytes();index=folder/'index.sqlite3'
            parsed=controller.validate_package(header,job,raw,index,None)
            self.assertEqual(parsed['last_block'],job['end'])
            with self.assertRaisesRegex(ValueError,'PARENT'):
                controller.validate_package(header,job,raw,index,hash_(1))
            with sqlite3.connect(index) as db:db.execute("UPDATE transactions SET recipient=?",('0x'+'3'*40,))
            with self.assertRaisesRegex(ValueError,'TRANSACTIONS'):
                controller.validate_package(header,job,raw,index,None)

    def test_transport_settlement_keeps_original_reservation_and_bounds(self):
        class Spend:
            grant={'internet_read_krw_per_gib':200,'operation_krw':1}
        spend=Spend();spend.db=sqlite3.connect(':memory:')
        spend.db.execute('CREATE TABLE operations(kind TEXT,key TEXT,krw INTEGER,body TEXT)')
        body=json.dumps(dict(bytes=64*1024**2,digest=None,usd=None),sort_keys=True)
        spend.db.execute('INSERT INTO operations VALUES(?,?,?,?)',('read','attempt',14,body));spend.db.commit()
        controller.settle_transport(spend,'attempt',1024)
        row=spend.db.execute('SELECT krw,body FROM operations').fetchone()
        self.assertEqual(row,(2,body))
        with self.assertRaises(ValueError):controller.settle_transport(spend,'attempt',64*1024**2)
        with self.assertRaises(ValueError):controller.settle_transport(spend,'unknown',1)
        spend.db.close()

    def test_plan_requires_full_nonoverlapping_frozen_cover(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'evidence').mkdir();parent=root/'evidence/grant.json';parent.write_text('{}')
            p=dict(chain_id=42161,monthly_limit_krw=1000000,
                parent_sha256=hashlib.sha256(parent.read_bytes()).hexdigest(),frozen_provider_head=22207821,
                ranges=[dict(lane=0,start=0,end=22207817)]+[dict(lane=i,start=22207817+i,end=22207817+i) for i in range(1,5)])
            path=root/'evidence/parallel-plan.json'
            def save():
                path.write_text(json.dumps(p));path.with_suffix('.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest())
            save();self.assertEqual(load_plan(root),p)
            p['ranges'][2]['start']-=1;save()
            with self.assertRaisesRegex(ValueError,'OVERLAP'):load_plan(root)


if __name__=='__main__':unittest.main()
