import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from economic_machine.resident.store import Store, Rejected
from economic_machine.resident.worker import Gate, Worker


class Model:
    def __init__(self): self.calls=0
    def infer(self,task,timeout=25):
        self.calls+=1
        return {'action':'note','topic':'personal','summary':'test'}, {'seconds':0.01}


class ResidentTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'state.sqlite')
        self.store.provision([{'id':f'a{i}','venue':f'v{i%4}', 'tools':['triage','note','remind','signal']} for i in range(100)])
        self.now=int(time.time())
        self.model=Model()
        self.gate=Gate(os.environ['RESIDENT_NATIVE'],os.environ['RESIDENT_NATIVE_SHA256'])
        self.worker=Worker(self.store,self.model,self.gate)

    def tearDown(self): self.tmp.cleanup()

    def task(self,agent='a0',key='one',kind='triage',text='hello'):
        return {'agent':agent,'key':key,'kind':kind,'payload':{'text':text},'due':self.now,'expires':self.now+600}

    def signal(self,price=10001):
        return {**self.task(kind='signal'),'expires':self.now+5,'payload':{
            'venue':'v0','sequence':1,'observed':self.now,'price':price,'reference':10000,'threshold_bps':10}}

    def test_100_identities(self): self.assertEqual(self.store.status()['agents'],100)
    def test_duplicate_identity(self):
        with self.assertRaises(Rejected): self.store.provision([{'id':'a0','venue':'x','tools':['note']}])
    def test_101_identity(self):
        with self.assertRaises(Rejected): self.store.provision([{'id':'extra','venue':'x','tools':['note']}])
        self.assertEqual(self.store.status()['agents'],100)
    def test_unknown_agent(self):
        with self.assertRaises(Rejected): self.store.submit(self.task('missing'))
    def test_unknown_tool(self):
        with self.assertRaises(Rejected): self.store.submit(self.task(kind='shell'))
    def test_scope_cannot_be_extended(self):
        with self.assertRaises(Rejected): self.store.submit({**self.task(),'tools':['shell']})
    def test_idempotent(self):
        task=self.task()
        self.assertEqual(self.store.submit(task),self.store.submit(task))
    def test_late_retry_resolves_completed_outcome(self):
        task=self.task(); job=self.store.submit(task)
        self.worker.step()
        self.assertEqual(self.store.submit(task,now=self.now+1000),job)
        self.assertEqual(self.store.status()['inference_reserved'],1)
    def test_conflicting_idempotency(self):
        self.store.submit(self.task())
        with self.assertRaises(Rejected): self.store.submit(self.task(text='different'))
    def test_bounded_queue(self):
        self.store.submit(self.task(key='1')); self.store.submit(self.task(key='2'))
        with self.assertRaises(Rejected): self.store.submit(self.task(key='3'))
    def test_fairness(self):
        for i in range(100):
            for j in range(2): self.store.submit(self.task(agent=f'a{i}',key=str(j)))
        first=[self.store.claim()['agent'] for _ in range(100)]
        self.assertEqual(len(set(first)),100)
    def test_future_reminder(self):
        self.store.submit({**self.task(kind='remind'),'due':self.now+30})
        self.assertIsNone(self.store.claim(now=self.now))
        self.assertIsNotNone(self.store.claim(now=self.now+30))
    def test_expiration(self):
        job=self.store.submit(self.task())
        self.assertIsNone(self.store.claim(now=self.now+601))
        self.assertEqual(self.store.results('a0')[0]['status'],'EXPIRED')
    def test_recovery_and_attempt_cap(self):
        self.store.submit(self.task()); self.store.claim(); self.store.reserve_inference('a0')
        self.store.recover(); self.assertIsNotNone(self.store.claim()); self.store.recover()
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.status()['inference_reserved'],1)
    def test_model_reservation_persisted(self):
        self.store.submit(self.task()); self.worker.step()
        self.assertEqual(Store(self.store.path).status()['inference_reserved'],1)
    def test_model_note_is_not_an_authorized_action(self):
        self.store.submit(self.task()); self.worker.step()
        row=self.store.results('a0')[0]
        self.assertEqual(row['status'],'PROPOSED')
        result=json.loads(row['result'])
        self.assertTrue(result['review_required'])
        self.assertFalse(result['execution_authority'])
    def test_model_timeout_keeps_reservation(self):
        def fail(*args,**kwargs): raise TimeoutError()
        self.model.infer=fail
        self.store.submit(self.task()); self.worker.step()
        self.assertEqual(self.store.results('a0')[0]['status'],'FAILED')
        self.assertEqual(self.store.status()['inference_reserved'],1)
    def test_future_signal_rejected(self):
        task=self.signal(); task['payload']['observed']+=3
        with self.assertRaises(Rejected): self.store.submit(task,now=self.now)
    def test_note_avoids_llm(self):
        self.store.submit(self.task(kind='note',text='$(do not execute)'))
        self.worker.step(); self.assertEqual(self.model.calls,0)
        self.assertIn('$(do not execute)',self.store.results('a0')[0]['result'])
    def test_agent_result_isolation(self):
        self.store.submit(self.task()); self.worker.step()
        self.assertEqual(self.store.results('a1'),[])
    def test_daily_budget(self):
        for _ in range(60): self.assertTrue(self.store.reserve_inference('a0',now=self.now))
        self.assertFalse(self.store.reserve_inference('a0',now=self.now))
        self.assertTrue(self.store.reserve_inference('a1',now=self.now))
    def test_fast_lane_bypasses_model(self):
        self.store.submit(self.task()); self.store.submit(self.task('a1',kind='note'))
        self.worker.step('fast'); self.assertEqual(self.model.calls,0)
        self.assertEqual(self.store.results('a0')[0]['status'],'QUEUED')
    def test_stale_signal(self):
        task=self.signal(); task['payload']['observed']-=8
        with self.assertRaises(Rejected): self.store.submit(task)
    def test_signal_wrong_venue(self):
        task=self.signal(); task['payload']['venue']='v1'
        with self.assertRaises(Rejected): self.store.submit(task)
    def test_signal_replay(self):
        self.store.submit(self.signal())
        with self.assertRaises(Rejected): self.store.submit({**self.signal(),'key':'replay'})
    def test_native_quiet_signal(self):
        self.store.submit(self.signal()); self.worker.step('fast')
        self.assertEqual(self.model.calls,0)
        self.assertEqual(json.loads(self.store.results('a0')[0]['result'])['action'],'ignore')
    def test_native_crossing(self):
        self.store.submit(self.signal(10010)); self.worker.step('fast')
        self.assertEqual(self.model.calls,0)
        self.assertEqual(self.store.results('a0')[0]['status'],'ESCALATED')
    def test_native_math_reference(self):
        rng=random.Random(49)
        for _ in range(10000):
            p,r,t=rng.randint(1,2**63-1),rng.randint(1,2**63-1),rng.randint(1,10000)
            self.assertEqual(self.gate.crossed({'price':p,'reference':r,'threshold_bps':t}),int(abs(p-r)*10000>=r*t))
    def test_no_duplicate_commit(self):
        job=self.store.submit(self.task()); self.worker.step()
        with self.assertRaises(Rejected): self.store.finish(job,'DONE',{})


if __name__=='__main__': unittest.main()
