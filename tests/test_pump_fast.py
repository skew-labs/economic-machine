import copy
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import socket
import tempfile
import unittest
from economic_machine.pump_research import fast_policy as policy
from economic_machine.pump_research.fast_native import Library,Engine,encode_event,integer
from economic_machine.pump_research.strategy import buy_quote,sell_quote,features
from economic_machine.pump_research.planner import validate as plan_validate
from economic_machine.pump_research.fast_stream import marker,Funding,percentiles
from economic_machine.pump_research.fast_bus import packet,unpack,Bus
from economic_machine.pump_research.store import Store
from test_pump_strategy import event,key,launch

def p():
    return dict(parameters=policy.CATALOG['persistent_demand'],version=1,expires_ms=2000000)
def rows():return [event(101+i*2,key(10+i)) for i in range(6)]

class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib=Library(os.environ['MACHINE_PUMP_LIB'])
    def setUp(self):self.e=Engine(self.lib,p(),launch())
    def tearDown(self):self.e.close()
    def seed(self):
        for e in rows():self.e.ingest(e)
    def test_candidate_is_not_execution(self):
        self.seed();r=self.e.decide(112,1000)
        self.assertEqual(r['reason'],'candidate');self.assertFalse(r['execution_authority']);self.assertFalse(r['history_complete'])
        self.assertLess(r['quote_exit_lamports'],50000000)
    def test_future_events_do_not_change_demand(self):
        self.seed();self.e.ingest(event(112,key(100)))
        self.assertEqual(self.e.decide(112,1000)['events'],6)
    def test_duplicate_and_conflict(self):
        self.seed();e=rows()[0];self.e.ingest(e);self.assertEqual(self.e.decide(112,1000)['events'],6)
        e['token_amount']+=1;self.e.ingest(e);self.assertEqual(self.e.decide(112,1000)['reason'],'bad_input')
    def test_funding_known_before_launch_only(self):
        self.seed()
        for e in rows():self.assertFalse(self.e.funding(e['user'],key(90),100))
        self.assertEqual(self.e.decide(112,1000)['reason'],'candidate')
        for e in rows():self.assertTrue(self.e.funding(e['user'],key(90),99))
        self.assertEqual(self.e.decide(112,1000)['reason'],'funding_concentration')
    def test_future_funding_conflict_invalidates(self):
        self.seed();self.e.funding(key(10),key(90),99);self.e.funding(key(10),key(91),99)
        self.assertEqual(self.e.decide(112,1000)['reason'],'invalidated')
    def test_invalidated_and_expired(self):
        self.seed();self.assertEqual(self.e.decide(112,2000000)['reason'],'policy_expired')
        self.e.invalidate();self.assertEqual(self.e.decide(112,1000)['reason'],'invalidated')
    def test_atomic_roundtrip_even_small(self):
        self.seed();e=event(111,key(15),False);e['signature']=rows()[-1]['signature'];e['ix']=1;e['sol_amount']=1;e['fee']=1;e['creator_fee']=1
        self.e.ingest(e);self.assertEqual(self.e.decide(112,1000)['reason'],'roundtrip')
    def test_fee_or_mode_unsupported_poisons_cohort(self):
        self.seed();e=event(111,key(99));e['fee']+=1
        self.e.ingest(e);self.assertEqual(self.e.decide(112,1000)['reason'],'unsupported')
    def test_unsigned_abi_does_not_wrap(self):
        for x in (-1,2**64,True,1.5):
            with self.assertRaises(ValueError):integer(x)
    def message(self):
        self.seed();r=self.e.decide(112,1000);r.update(mint=key(1),source_reference={'raw_sha256':'a'*64})
        return r
    def test_bus_wire_and_non_authority(self):
        r=self.message();raw=packet(r,1,1000,2000);self.assertEqual(len(raw),232)
        out=unpack(raw,1001);self.assertEqual(out['policy_version'],1);self.assertEqual(out['net_lamports'],r['net_lamports'])
        self.assertFalse(out['execution_authority'])
    def test_bus_expiry_and_corruption(self):
        raw=packet(self.message(),1,1000,2000)
        for data,at in ((raw,2000),(raw,999),(raw[:-1],1000),(b'BAD!'+raw[4:],1000)):
            with self.assertRaises(ValueError):unpack(data,at)
    def test_bus_rejects_authority_and_long_ttl(self):
        r=self.message()
        with self.assertRaises(ValueError):packet(r,1,1000,2001)
        r['execution_authority']=1
        with self.assertRaises(ValueError):packet(r,1,1000,2000)
    def test_bus_native_packet_delivery_and_disconnect(self):
        r=self.message()
        with tempfile.TemporaryDirectory() as d:
            bus=Bus(Path(d)/'signals.sock');c=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
            c.settimeout(1);c.connect(str(Path(d)/'signals.sock'));bus.accept()
            bus.publish(r,1,1000,2000);self.assertEqual(unpack(c.recv(512),1001)['kind'],1)
            bus.publish(r,2,1001,2001);self.assertEqual(unpack(c.recv(512),1002)['kind'],2)
            c.close();bus.publish(r,1,1002,2002);self.assertEqual(bus.stats['backpressure_or_disconnected'],1);bus.close()
    def test_small_percentile_monotonic(self):
        for n in (1,2,3,10):
            r=percentiles(list(range(n)));self.assertLessEqual(r['p50'],r['p95']);self.assertLessEqual(r['p95'],r['p99'])
    def test_randomized_quote_reference_2000(self):
        rng=random.Random(467)
        for _ in range(2000):
            e=event();e['virtual_sol_reserves']=e['virtual_quote_reserves']=rng.randint(10**9,10**13)
            e['virtual_token_reserves']=rng.randint(10**11,10**17);e['real_token_reserves']=e['virtual_token_reserves']//2
            e['real_sol_reserves']=e['real_quote_reserves']=e['virtual_sol_reserves']//2
            e['fee_basis_points']=rng.randint(0,500);e['creator_fee_basis_points']=rng.randint(0,100)
            e['fee']=(e['sol_amount']*e['fee_basis_points']+9999)//10000;e['creator_fee']=(e['sol_amount']*e['creator_fee_basis_points']+9999)//10000
            budget=rng.randint(10000,50000000);q=encode_event(e);tokens=C.c_uint64();proceeds=C.c_uint64()
            self.assertTrue(self.lib.lib.machine_pump_quotes(C.byref(q),budget,C.byref(tokens),C.byref(proceeds)))
            expected=buy_quote(e,budget);self.assertEqual(tokens.value,expected);self.assertEqual(proceeds.value,sell_quote(e,expected))
    def test_randomized_feature_reference_100(self):
        rng=random.Random(87)
        for _ in range(100):
            engine=Engine(self.lib,p(),launch());events=[]
            for i in range(30):
                e=event(rng.randint(100,111),key(rng.randint(10,30)),rng.random()<.7)
                e.update(signature=str(i),ix=i,sol_amount=rng.randint(1,10**8));e['fee']=(e['sol_amount']*95+9999)//10000;e['creator_fee']=(e['sol_amount']*30+9999)//10000
                engine.ingest(e);events.append(e)
            r=engine.decide(112,1000);f=features(events,launch(),112)
            for a,b in [('events','events'),('buyers','buyer_wallets'),('buy_lamports','buy_lamports'),('net_lamports','net_lamports'),('buyer_bps','largest_buyer_bps'),('roundtrip_bps','roundtrip_bps')]:self.assertEqual(r[a],f[b])
            engine.close()

class PolicyTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def artifact(self):return policy.install(self.root,'persistent_demand',1,'a'*64,1000,10000000)
    def test_install_and_catalog_frozen(self):
        x=self.artifact();self.assertEqual(policy.validate(x,2000,10000000),x)
        self.assertEqual(x['expires_ms'],3601000)
    def test_expiration_and_budget_deadline(self):
        x=self.artifact()
        for now in (999,3601000):
            with self.assertRaises(ValueError):policy.validate(x,now,10000000)
        with self.assertRaises(ValueError):policy.validate(x,2000,2500)
    def test_model_cannot_change_threshold_or_authority(self):
        original=self.artifact()
        for field,value in [('execution_authority',True),('profitability_proven',True),('model','other')]:
            x=copy.deepcopy(original);x[field]=value
            with self.assertRaises(ValueError):policy.validate(x,2000,10000000)
        x=copy.deepcopy(original);x['parameters']['buyers']=1
        with self.assertRaises(ValueError):policy.validate(x,2000,10000000)
    def test_policy_version_cannot_roll_back(self):
        self.artifact()
        with self.assertRaises(ValueError):self.artifact()
    def test_unknown_profile(self):
        with self.assertRaises(ValueError):policy.install(self.root,'shell',1,'a'*64,1000,10000)
    def test_policy_requires_cost_reconciled_response(self):
        self.artifact();db=sqlite3.connect(self.root/'model-budget.sqlite3')
        db.execute('CREATE TABLE calls(id INTEGER,status TEXT,response_hash TEXT,actual TEXT)')
        db.execute('INSERT INTO calls VALUES(1,?,?,?)',('valid','a'*64,None));db.commit()
        with self.assertRaises(ValueError):policy.load(self.root,2000,10000000)
        db.execute("UPDATE calls SET actual='0.001'");db.commit();db.close()
        self.assertEqual(policy.load(self.root,2000,10000000)['version'],1)
    @unittest.skipUnless(Path('/proc/self/fd').exists(),'Linux descriptor regression')
    def test_policy_poll_closes_descriptors_without_garbage_collection(self):
        import gc
        self.artifact()
        db=sqlite3.connect(self.root/'model-budget.sqlite3')
        db.execute('CREATE TABLE calls(id INTEGER,status TEXT,response_hash TEXT,actual TEXT)')
        db.execute('INSERT INTO calls VALUES(1,?,?,?)',('valid','a'*64,'0.001'));db.commit();db.close()
        gc.collect();before=len(list(Path('/proc/self/fd').iterdir()));was=gc.isenabled();gc.disable()
        try:
            for _ in range(1100):policy.load(self.root,2000,10000000)
            self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())),before+1)
        finally:
            if was:gc.enable()
            gc.collect()
    def test_planner_catalog_selection(self):
        plan=dict(candidate_ids=['a'],hypotheses=['test'],limitations=['test'],signal_profile='patient_demand')
        self.assertEqual(plan_validate(plan,{'a':{}}),plan)
        plan['signal_profile']='execute'
        with self.assertRaises(ValueError):plan_validate(plan,{'a':{}})
    def test_marker_rejects_path_traversal_and_negative(self):
        f=self.root/'marker.json'
        for value in (dict(segment='../a',offset=0,records=0),dict(segment='00000001',offset=-1,records=0)):
            f.write_text(json.dumps(value))
            f.chmod(0o600)
            with self.assertRaises(ValueError):marker(f)
    def test_marker_symlink_rejected(self):
        f=self.root/'marker.json';f.symlink_to('/dev/null')
        with self.assertRaises(OSError):marker(f)
    def test_marker_pins_producer_uid_not_consumer_uid(self):
        f=self.root/'marker.json';value=dict(segment='00000001',offset=0,records=0)
        f.write_text(json.dumps(value));f.chmod(0o600)
        self.assertEqual(marker(f,os.geteuid()),value)
        with self.assertRaises(ValueError):marker(f,os.geteuid()+1)
        f.chmod(0o644)
        with self.assertRaises(ValueError):marker(f,os.geteuid())
    def test_feedback_ignores_external_strings(self):
        value=dict(schema='economic-machine.pump-fast-status.v1',execution_authority=False,updated_ms=1000,policy_version=5,
                   counts={'decisions':10,'url':'untrusted'},reasons={'candidate':2,'execute':'untrusted'},instruction='arbitrary external text')
        (self.root/'fast-status.json').write_text(json.dumps(value))
        r=policy.feedback(self.root,2000);self.assertEqual(r['counts']['decisions'],10)
        self.assertNotIn('untrusted',json.dumps(r));self.assertTrue(r['observations_are_not_outcomes'])
        value['updated_ms']=3000;(self.root/'fast-status.json').write_text(json.dumps(value))
        with self.assertRaises(ValueError):policy.feedback(self.root,2000)

if __name__=='__main__':unittest.main()
