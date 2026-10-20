from historical_scope import HistoricalScopeTestCase
import contextlib
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('probe',Path(__file__).resolve().parents[1]/'products/arbitrum_research/freezer_transport_probe.py')
m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)
DATA=b'x'*m.SIZE

def ticket():
    return dict(schema='economic-machine.public-range-probe.v1',id='a'*32,site='tokyo',bytes=m.SIZE,
        relative=32,expires_ms=min(m.DEADLINE,time.time_ns()//1000000+60000),expected_sha256=hashlib.sha256(DATA).hexdigest(),
        part=dict(url='https://snapshot.arbitrum.io/arb1/fixed/full-path.tar.part0000',bytes=32+m.SIZE),
        pin=dict(effective_url='https://snapshot.arbitrum.foundation/arb1/fixed/full-path.tar.part0000',bytes=32+m.SIZE,etag='"fixed"'))

class Response:
    def __init__(self,t,data=DATA,status=206):
        self.data=data;self.status=status
        self.headers={'Content-Range':f"bytes 32-{31+m.SIZE}/{32+m.SIZE}",'ETag':'"fixed"'}
    def read(self,n):return self.data[:n]

class Connection:
    def __init__(self,response):self.response=response;self.closed=False;self.requests=[]
    def request(self,*a,**kw):self.requests.append((a,kw))
    def getresponse(self):return self.response
    def close(self):self.closed=True

class ProbeTests(HistoricalScopeTestCase):
    def test_ticket_pins(self):
        self.assertEqual(m.validate_ticket(ticket()).hostname,'snapshot.arbitrum.foundation')
    def test_invalid_ticket_scope(self):
        for key,value in [('bytes',m.SIZE+1),('relative',-1),('site','other'),('expires_ms',m.DEADLINE+1),('id','../escape')]:
            t=ticket();t[key]=value
            with self.assertRaises(ValueError):m.validate_ticket(t)
    def test_deadline(self):
        t=ticket();t['expires_ms']=m.DEADLINE
        with self.assertRaises(ValueError):m.validate_ticket(t,now_ms=m.DEADLINE)
    def test_url_and_etag_injection(self):
        for url in ['http://snapshot.arbitrum.foundation/arb1/fixed/full-path.tar.part0000','https://evil.example/x','https://snapshot.arbitrum.foundation/other','https://snapshot.arbitrum.foundation/arb1/fixed/full-path.tar.part0000?key=bad']:
            t=ticket();t['pin']['effective_url']=url
            with self.assertRaises(ValueError):m.validate_ticket(t)
        t=ticket();t['pin']['etag']='"a"\r\nX: y'
        with self.assertRaises(ValueError):m.validate_ticket(t)
    def run_fetch(self,response):
        t=ticket();con=Connection(response);temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        with patch.object(m.shutil,'disk_usage',return_value=type('D',(),{'free':200*1024**3})()):
            r=m.fetch(t,Path(temp.name),lambda *a,**k:con)
        self.assertEqual(len(con.requests),1);self.assertTrue(con.closed)
        return t,r,Path(temp.name)
    def test_success_full_retained_and_no_replay(self):
        t,r,out=self.run_fetch(Response(ticket()))
        self.assertEqual(m.validate_receipt(t,r),r)
        self.assertEqual((out/(t['id']+'.raw')).read_bytes(),DATA)
        with self.assertRaises(ValueError):m.fetch(t,out)
    def test_etag_mismatch(self):
        res=Response(ticket());res.headers['ETag']='"changed"'
        t,r,_=self.run_fetch(res);self.assertEqual(r['state'],'VALIDATION_ERROR')
        with self.assertRaises(ValueError):m.validate_receipt(t,r)
    def test_length_and_hash_mismatch_retained(self):
        for data in [DATA[:-1],b'z'*m.SIZE,DATA+b'z']:
            t,r,out=self.run_fetch(Response(ticket(),data=data));self.assertEqual(r['state'],'VALIDATION_ERROR')
            self.assertTrue((out/(t['id']+'.raw')).exists())
            with self.assertRaises(ValueError):m.validate_receipt(t,r)
    def test_status_no_redirect_or_retry(self):
        for status in [301,403,429,503]:
            res=Response(ticket(),status=status);res.headers['Retry-After']='999'
            t,r,_=self.run_fetch(res)
            with self.assertRaises(urllib.error.HTTPError) as got:m.validate_receipt(t,r)
            self.assertEqual(got.exception.code,status);self.assertEqual(got.exception.headers['Retry-After'],'999')
    def test_transport_error_unknown(self):
        res=Response(ticket());res.read=lambda n:(_ for _ in ()).throw(TimeoutError())
        t,r,_=self.run_fetch(res);self.assertEqual(r['state'],'TRANSPORT_ERROR')
        with self.assertRaises(urllib.error.URLError):m.validate_receipt(t,r)
    def test_central_lease_and_reservation_order(self):
        log=[];held=False
        @contextlib.contextmanager
        def gate():
            nonlocal held
            held=True;log.append('enter')
            try:yield
            finally:held=False;log.append('exit')
        def guard():log.append('guard')
        def reserve():self.assertTrue(held);log.append('reserve');return 'id'
        def request(i):self.assertTrue(held);log.append('request');return 'receipt'
        def commit(i,r):self.assertTrue(held);log.append('commit')
        self.assertEqual(m.admitted(guard,gate(),reserve,request,commit),'receipt')
        self.assertEqual(log,['guard','enter','guard','reserve','request','guard','commit','exit'])
    def test_timeout_preserves_reservation_no_commit(self):
        reservations=[];commits=[]
        def req(i):raise urllib.error.URLError('UNKNOWN')
        with self.assertRaises(urllib.error.URLError):m.admitted(lambda:None,contextlib.nullcontext(),lambda:reservations.append('reserved'),req,lambda *x:commits.append(x))
        self.assertEqual(reservations,['reserved']);self.assertFalse(commits)
    def test_gate_receives_original_429(self):
        observed=[]
        @contextlib.contextmanager
        def gate():
            try:yield
            except urllib.error.HTTPError as e:observed.append((e.code,e.headers['Retry-After']));raise
        def req(i):raise m.response_error(dict(state='HTTP_ERROR',status=429,retry_after='120'))
        with self.assertRaises(urllib.error.HTTPError):m.admitted(lambda:None,gate(),lambda:'reserved',req,lambda *x:None)
        self.assertEqual(observed,[(429,'120')])
    def test_guard_after_response_blocks_commit(self):
        counter=iter([None,None,ValueError('EXPIRED')]);commits=[]
        def guard():
            result=next(counter)
            if result:raise result
        with self.assertRaises(ValueError):m.admitted(guard,contextlib.nullcontext(),lambda:'reserved',lambda x:'body',lambda *x:commits.append(x))
        self.assertFalse(commits)
    def test_original_shared_gate_preserves_retry_after(self):
        from economic_machine.pump_research.archive_gate import ArchiveGate, ArchiveDeferred
        with tempfile.TemporaryDirectory() as tmp:
            before=time.time()
            with self.assertRaises(urllib.error.HTTPError):
                with ArchiveGate(tmp,lambda:None,wait=False):
                    raise m.response_error(dict(state='HTTP_ERROR',status=429,retry_after='123'))
            saved=json.loads((Path(tmp)/'network-state.json').read_bytes())
            self.assertGreaterEqual(saved['not_before'],before+123)
            self.assertTrue(saved['failed_reservation_retained'])
            with self.assertRaises(ArchiveDeferred):
                with ArchiveGate(tmp,lambda:None,wait=False):self.fail('cooldown bypassed')
    def test_original_shared_gate_excludes_second_controller(self):
        from economic_machine.pump_research.archive_gate import ArchiveGate, ArchiveDeferred
        with tempfile.TemporaryDirectory() as tmp:
            with ArchiveGate(tmp,lambda:None,wait=False):
                with self.assertRaises(ArchiveDeferred):
                    with ArchiveGate(tmp,lambda:None,wait=False):self.fail('lease bypassed')

if __name__=='__main__':unittest.main()
