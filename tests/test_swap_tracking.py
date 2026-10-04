import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from machine_commerce.gas_portal import SwapStore, create_gas_portal
from machine_commerce.gas_router import GasRouter
from machine_commerce.swap_tracking import SwapTracker
from test_gas_router import FakeNetwork, TEST, sign
from fastapi.testclient import TestClient


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.net=FakeNetwork()
        self.router=GasRouter(self.net.read,self.net.api,lambda:self.net.now)
        self.store=SwapStore(Path(self.folder.name)/'swaps.sqlite3',self.router)
        self.p=self.store.call('quote',{'owner':TEST.address,'amount_atoms':'2000000'})
        self.p=self.store.call('order',{'id':self.p['id'],'signature':sign(self.p['permit'])})
        self.net.intent=self.p
        self.tracker=SwapTracker(self.store)

    def submit(self):
        self.store.call('submit',{'id':self.p['id'],'signature':sign(self.p['order'])})

    def test_restart_adopts_order_and_finalized_receipt_stops_reads(self):
        self.submit();self.net.provider_status='fulfilled'
        first=SwapTracker(self.store);self.assertEqual(first.tick(),1)
        result=SwapTracker(self.store).watch(self.p['id'])
        self.assertTrue(result['chain_verified']);self.assertFalse(result['tracking']['active'])
        count=len(self.net.calls);self.net.now+=1000
        self.assertEqual(first.tick(),0);first.watch(self.p['id'])
        self.assertEqual(len(self.net.calls),count);self.assertEqual(self.net.posts,1)

    def test_finality_and_proof_flags_are_required(self):
        self.submit();phase=[{'status':'AWAITING_FINALITY','chain_verified':False}]
        t=SwapTracker(self.store,lambda sid:phase[0])
        t.tick();self.assertTrue(t.watch(self.p['id'])['tracking']['active'])
        self.net.now+=31;phase[0]={'status':'FILLED_FINALIZED','chain_verified':False};t.tick()
        self.assertTrue(t.watch(self.p['id'])['tracking']['active'])
        self.net.now+=11;phase[0]={'status':'FILLED_FINALIZED','chain_verified':True};t.tick()
        self.assertFalse(t.watch(self.p['id'])['tracking']['active']);self.assertEqual(self.net.posts,1)

    def test_source_failure_backs_off_and_never_leaks_or_resubmits(self):
        self.submit()
        def fail(sid):raise RuntimeError('secret-rpc-token and private provider payload')
        t=SwapTracker(self.store,fail);t.tick();result=t.watch(self.p['id'])
        self.assertNotIn('secret',json.dumps(result));self.assertFalse(result['safe_to_retry'])
        self.assertEqual(result['tracking']['source_error'],'TRACKING_SOURCE_UNAVAILABLE')
        self.assertGreater(result['tracking']['next_check'],self.net.now)
        self.assertEqual(t.tick(),0);self.assertEqual(self.net.posts,1)

    def test_worker_crash_recovers_expired_lease(self):
        self.submit()
        def crash(sid):raise SystemExit('fixture worker crash')
        with self.assertRaises(SystemExit):SwapTracker(self.store,crash).tick()
        restarted=SwapTracker(self.store,lambda sid:{'status':'PENDING'})
        self.assertEqual(restarted.tick(),0)
        self.net.now+=181;self.assertEqual(restarted.tick(),1)
        self.assertEqual(restarted.watch(self.p['id'])['tracking']['checks'],1)
        self.assertEqual(self.net.posts,1)

    def test_multiple_workers_do_not_duplicate_due_check(self):
        self.submit();entered=threading.Event();release=threading.Event();calls=[]
        def check(sid):
            calls.append(sid);entered.set();release.wait(5);return {'status':'PENDING'}
        with ThreadPoolExecutor(2) as pool:
            future=pool.submit(SwapTracker(self.store,check).tick,1)
            self.assertTrue(entered.wait(2))
            try:self.assertEqual(SwapTracker(self.store,check).tick(1),0)
            finally:release.set()
            self.assertEqual(future.result(),1)
        self.assertEqual(calls,[self.p['id']]);self.assertEqual(self.net.posts,1)

    def test_unreceived_submission_recovers_only_after_expiry_proof(self):
        # Order was exposed for signing, but a submit request never arrived.
        self.tracker.tick();r=self.tracker.watch(self.p['id'])
        self.assertTrue(r['tracking']['active']);self.assertFalse(r['safe_to_retry'])
        self.net.now=self.p['valid_to']+1;self.tracker.tick()
        r=self.tracker.watch(self.p['id']);self.assertTrue(r['safe_to_retry'])
        self.assertEqual(r['status'],'EXPIRED_UNFILLED');self.assertFalse(r['tracking']['active'])
        self.assertEqual(self.net.posts,0)

    def test_watch_http_does_not_query_rpc_or_request_signature(self):
        self.tracker.adopt();count=len(self.net.calls)
        # No lifespan: explicitly exercise the cache route without starting a worker.
        client=TestClient(create_gas_portal(self.store))
        r=client.post('/commerce/swap-api/watch',headers={'Origin':'https://machine.148-113-153-116.nip.io'},json={'id':self.p['id']})
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['tracking']['authority'],'READ_ONLY')
        self.assertEqual(len(self.net.calls),count)
        self.assertEqual(client.post('/commerce/swap-api/watch',json={'id':self.p['id']}).status_code,403)
        client.close()
