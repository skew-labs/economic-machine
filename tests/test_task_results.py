import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from fastapi.testclient import TestClient
from economic_machine.values import MachineError
from machine_engine.workspace import Workspace
from machine_engine.task_results import LocalWork
from machine_engine.assistant import Assistant, BedrockIntake
from machine_engine.api import create_engine_app
from test_tasks import brief
from test_engine_workspace import OWNER_TOKEN

class LocalWorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=1791000000
        self.work=Workspace(Path(self.tmp.name)/'work.db',clock=lambda:self.now)
        self.task=self.work.tasks.create(brief(kind='data_cleanup',constraints={'required_fields':['name','email'],'output_format':'csv'},budget={'currency':'USD','maximum':'0'}))
        self.runner=LocalWork(self.work)
        self.raw={'request_id':'fixture-local-plan','expected_revision':1,'input':{'csv':'name,email\n Alex , a@b.com \nAlex,a@b.com\n=1+1,evil\n'}}
        self.job=self.runner.plans(self.task['id'],self.raw)
    def tearDown(self):self.tmp.cleanup()
    def test_two_real_plans_produce_different_bounded_results(self):
        self.assertEqual(len(self.job['plans']),2)
        self.assertNotIn('input',self.job['plans'][0])
        result=self.runner.run(self.job['id'],{'plan_hash':self.job['plans'][1]['hash']})
        self.assertEqual(result['status'],'DELIVERED')
        output=self.runner.result(self.job['id']);self.assertEqual(output['rows'],2);self.assertEqual(output['removed_rows'],1)
        self.assertIn("'=1+1",output['content']);self.assertEqual(output['external_charge_cents'],0)
        self.assertFalse(self.work.task_checkout.status()['purchases'])
    def test_concurrent_retry_is_one_result_and_resumes_after_restart(self):
        raw={'plan_hash':self.job['plans'][0]['hash']}
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.runner.run(self.job['id'],raw),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.runner.result(self.job['id'])['rows'],3)
        restored=LocalWork(Workspace(Path(self.tmp.name)/'work.db',clock=lambda:self.now))
        self.assertEqual(restored.result(self.job['id']),self.runner.result(self.job['id']))
        with self.assertRaisesRegex(MachineError,'DIFFERENT_PLAN'):self.runner.run(self.job['id'],{'plan_hash':self.job['plans'][1]['hash']})
    def test_wrong_owner_expiry_tamper_and_changed_input_fail_closed(self):
        foreign=LocalWork(Workspace(Path(self.tmp.name)/'other.db'))
        with self.assertRaisesRegex(MachineError,'NOT_FOUND'):foreign.run(self.job['id'],{'plan_hash':self.job['plans'][0]['hash']})
        self.now+=901
        with self.assertRaisesRegex(MachineError,'EXPIRED'):self.runner.run(self.job['id'],{'plan_hash':self.job['plans'][0]['hash']})
        with self.assertRaisesRegex(MachineError,'CONFLICT'):self.runner.plans(self.task['id'],self.raw|{'input':{'csv':'changed'}})
    def test_revision_invalidates_plan(self):
        revised=brief('revise',kind='data_cleanup',constraints={'required_fields':['name','email'],'output_format':'json'},budget={'currency':'USD','maximum':'0'})
        revised.pop('request_id')
        self.work.tasks.revise(self.task['id'],{'request_id':'revise-test','expected_revision':1,'draft':revised})
        with self.assertRaisesRegex(MachineError,'REVISED'):self.runner.run(self.job['id'],{'plan_hash':self.job['plans'][0]['hash']})
    def test_http_result_requires_owner_and_validated_completion(self):
        client=TestClient(create_engine_app(Path(self.tmp.name)/'work.db',admin_token=OWNER_TOKEN,workspace=self.work))
        path='/api/engine/local-work/'+self.job['id']
        self.assertEqual(client.get(path+'/result').status_code,401)
        headers={'Authorization':'Bearer '+OWNER_TOKEN}
        self.assertEqual(client.get(path+'/result',headers=headers).status_code,409)
        self.assertEqual(client.post(path+'/run',headers=headers,json={'plan_hash':self.job['plans'][0]['hash']}).status_code,200)
        self.assertEqual(client.get(path+'/result',headers=headers).status_code,200);client.close()
    def test_bedrock_org_deny_does_not_call_other_provider(self):
        with patch.dict('os.environ',{'SKEW_BEDROCK_ACCESS_STATUS':'ORGANIZATION_DENY','SKEW_ASSISTANT_PROVIDER':'bedrock'}),patch('machine_engine.assistant.QwenIntake.complete') as fallback:
            agent=Assistant(self.work)
            self.assertIsInstance(agent.provider,BedrockIntake)
            with self.assertRaisesRegex(MachineError,'ORGANIZATION_DENY'):agent.send({'request_id':'blocked-request-00001','message':'Help me clean my data'})
            fallback.assert_not_called()
    def test_bedrock_secret_never_enters_conversation_or_task(self):
        fake='ABSK'+'a'*40
        with self.assertRaisesRegex(MachineError,'PRIVATE_KEYS'):Assistant(self.work).send({'request_id':'secret-request-00001','message':fake})
        self.assertEqual(Assistant(self.work).history()['turns'],[])
        with self.assertRaises(MachineError):self.work.tasks.create(brief('secret-task',instructions=fake))
