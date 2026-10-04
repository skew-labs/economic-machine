import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from economic_machine.values import MachineError
from machine_engine.assistant import Assistant, QwenIntake, reserve_model_call, validate_proposal
from machine_commerce.api import create_app
from machine_engine.workspace import Workspace


class FixtureInterpreter:
    def __init__(self):
        self.calls = 0
        self.value = {'reply': 'Review this task.', 'action': 'task', 'amount': None,
            'task': {'kind': 'vendor_comparison', 'title': 'Compare CRM vendors', 'budget': '10',
                     'constraints': {'output_language': 'en', 'output_format': 'table',
                                     'comparison_fields': ['Price', 'Support']}, 'preference_id': None}}
    def complete(self, messages):
        self.calls += 1
        self.messages = messages
        return copy.deepcopy(self.value), {'model': 'test-provider', 'latency_ms': 1}


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Workspace(Path(self.tmp.name)/'owner.db')
        self.provider = FixtureInterpreter()
        self.agent = Assistant(self.work, self.provider)
        self.raw = {'request_id': 'request-000000000001', 'message': 'Compare CRM vendors within 10 USD.'}
    def tearDown(self):
        self.tmp.cleanup()
    def test_proposal_requires_review_and_task_acceptance_is_idempotent(self):
        result = self.agent.send(self.raw)
        self.assertEqual(result['authority'], 'PROPOSE_ONLY')
        self.assertEqual(len(self.work.tasks.status()['tasks']), 0)
        first = self.agent.accept_task(self.raw['request_id'])
        second = self.agent.accept_task(self.raw['request_id'])
        self.assertEqual(first, second)
        self.assertEqual(first['payment_authority'], 'NONE')
        self.assertEqual(first['brief']['budget']['cents'], 1000)
    def test_retry_is_cached_and_conflicting_id_is_rejected(self):
        self.assertEqual(self.agent.send(self.raw), self.agent.send(self.raw))
        self.assertEqual(self.provider.calls, 1)
        with self.assertRaisesRegex(MachineError, 'CONFLICT'):
            self.agent.send(self.raw | {'message':'Different request'})
    def test_owner_isolation(self):
        self.agent.send(self.raw)
        other = Assistant(Workspace(Path(self.tmp.name)/'other.db'), self.provider)
        self.assertEqual(other.history()['turns'], [])
        with self.assertRaises(MachineError):
            other.accept_task(self.raw['request_id'])
    def test_missing_budget_cannot_be_accepted(self):
        self.provider.value['task']['budget'] = None
        self.agent.send(self.raw)
        with self.assertRaisesRegex(MachineError, 'BUDGET'):
            self.agent.accept_task(self.raw['request_id'])
    def test_unsupported_actions_and_overspend_are_rejected(self):
        for action, amount in [('send',None),('swap','0'),('swap','-1'),('swap',2),('swap','1e0'),('swap','0.0000001'),('swap','9'*73)]:
            with self.subTest(action=action,amount=amount), self.assertRaises(MachineError):
                validate_proposal({'reply':'Do it','action':action,'amount':amount,'task':None})

    def test_swap_amount_has_no_fixed_dollar_cap(self):
        for amount in ['0.000001','3.1','10','1000000.123456']:
            self.assertEqual(validate_proposal({'reply':'Review','action':'swap','amount':amount,'task':None})['amount'],amount)
    def test_provider_failure_is_durable_and_not_repeated(self):
        def fail(messages):
            raise ValueError('secret provider response')
        self.provider.complete=fail
        with self.assertRaisesRegex(MachineError, '^ASSISTANT_PROVIDER_UNAVAILABLE_OR_INVALID$'):
            self.agent.send(self.raw)
        self.assertEqual(self.agent.history()['turns'][0]['status'],'FAILED')
        with self.assertRaisesRegex(MachineError, 'ALREADY_ATTEMPTED'):
            self.agent.send(self.raw)
    def test_secret_is_rejected_before_persistence(self):
        with self.assertRaisesRegex(MachineError, 'PRIVATE_KEYS'):
            self.agent.send(self.raw | {'message':'key sk-'+'a'*40})
        self.assertEqual(self.agent.history()['turns'], [])
    def test_last_preferences_are_not_invented(self):
        self.provider.value['task']['preference_id']='last'
        self.agent.send(self.raw)
        task=self.agent.accept_task(self.raw['request_id'])
        self.assertEqual(task['status'],'NEEDS_INPUT')
        self.assertIn('confirmed_preferences',task['missing_information'])
    def test_daily_limit_counts_failed_attempts(self):
        with self.work.runtime.connect() as db:
            for i in range(40):db.execute("INSERT INTO engine_conversation VALUES (?,?,?,?, 'FAILED',NULL,NULL)",(str(i),'hash','message',int(self.work.clock())))
        with self.assertRaisesRegex(MachineError,'DAILY_LIMIT'):
            self.agent.send(self.raw)
        self.assertEqual(self.provider.calls,0)

    def test_http_owner_boundary_and_cross_workspace_history(self):
        app=create_app(Path(self.tmp.name)/'commerce.db')
        with TestClient(app) as owner, TestClient(app) as other, TestClient(app) as guest:
            owner.post('/api/sessions',json={});other.post('/api/sessions',json={})
            key=owner.post('/api/keys',json={'name':'Agent','scopes':['engine:read','engine:write'],
                'policy_id':None,'ttl_seconds':3600}).json()['secret']
            with patch('machine_engine.assistant.BedrockIntake',return_value=self.provider),patch.dict(os.environ,{'SKEW_ASSISTANT_PROVIDER':'bedrock'}):
                self.assertEqual(guest.post('/api/engine/assistant',json=self.raw).status_code,401)
                self.assertEqual(guest.post('/api/engine/assistant',json=self.raw,headers={'Authorization':'Bearer '+key}).status_code,403)
                self.assertEqual(owner.post('/api/engine/assistant',json=self.raw).status_code,200)
                self.assertEqual(other.get('/api/engine/assistant').json()['turns'],[])
                self.assertEqual(len(owner.get('/api/engine/assistant').json()['turns']),1)

    def test_qwen_transport_bounds_and_validated_result(self):
        class Response:
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def read(inner,limit):
                self.assertEqual(limit,64001)
                return json.dumps({'choices':[{'message':{'content':json.dumps(self.provider.value)}}],
                    'usage':{'prompt_tokens':40,'completion_tokens':30,'provider_timing':0.7}}).encode()
        env={'SKEW_ASSISTANT_BASE_URL':'https://provider.example/v1','SKEW_ASSISTANT_API_KEY':'fixture-key',
            'SKEW_ASSISTANT_MODEL':'qwen3-32b','SKEW_ASSISTANT_LIMIT_DB':self.tmp.name+'/calls.db'}
        with patch.dict(os.environ,env),patch('machine_engine.assistant.urllib.request.urlopen',return_value=Response()) as call:
            result,trace=QwenIntake().complete([{'role':'user','content':'CRM comparison'}])
            self.assertEqual(result,self.provider.value);self.assertEqual(trace['usage']['prompt_tokens'],40)
            args=json.loads(call.call_args.args[0].data)
            self.assertEqual(args['max_tokens'],600);self.assertFalse(args['chat_template_kwargs']['enable_thinking'])
            self.assertEqual(call.call_args.kwargs['timeout'],25)

    def test_qwen_global_attempt_cap_prevents_network_call(self):
        env={'SKEW_ASSISTANT_BASE_URL':'https://provider.example/v1','SKEW_ASSISTANT_API_KEY':'fixture-key',
            'SKEW_ASSISTANT_MODEL':'qwen3-32b','SKEW_ASSISTANT_LIMIT_DB':self.tmp.name+'/calls.db'}
        with patch.dict(os.environ,env),patch('machine_engine.assistant.urllib.request.urlopen') as call:
            for _ in range(200):reserve_model_call()
            with self.assertRaisesRegex(MachineError,'DAILY_LIMIT'):QwenIntake().complete([])
            call.assert_not_called()

    def test_qwen_failure_never_leaks_provider_details(self):
        env={'SKEW_ASSISTANT_BASE_URL':'https://provider.example/v1','SKEW_ASSISTANT_API_KEY':'fixture-key',
            'SKEW_ASSISTANT_MODEL':'qwen3-32b','SKEW_ASSISTANT_LIMIT_DB':self.tmp.name+'/calls.db'}
        with patch.dict(os.environ,env),patch('machine_engine.assistant.urllib.request.urlopen',side_effect=ValueError('provider-secret')):
            with self.assertRaisesRegex(MachineError,'^ASSISTANT_PROVIDER_UNAVAILABLE_OR_INVALID$'):QwenIntake().complete([])

if __name__ == '__main__': unittest.main()
