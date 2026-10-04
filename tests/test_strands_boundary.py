"""Real Strands orchestration with stubbed AWS responses; no provider success claim."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from botocore.stub import Stubber
from strands.models import BedrockModel
from machine_engine.assistant import BedrockIntake
from machine_engine.workspace import Workspace
from economic_machine.values import MachineError

class StrandsBoundaryTests(unittest.TestCase):
    def test_real_sdk_tool_loop_with_bounded_stubbed_converse(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Workspace(Path(tmp)/'work.db')
            env={'AWS_REGION':'us-east-1','SKEW_BEDROCK_MODEL_ID':'amazon.nova-lite-v1:0','SKEW_BEDROCK_ENABLED':'1',
                 'AWS_ACCESS_KEY_ID':'testing','AWS_SECRET_ACCESS_KEY':'testing','SKEW_ASSISTANT_LIMIT_DB':str(Path(tmp)/'calls.db'),
                 'AWS_EC2_METADATA_DISABLED':'true','SKEW_BEDROCK_ACCESS_STATUS':''}
            stubs=[]
            def model(**kwargs):
                instance=BedrockModel(**kwargs);stub=Stubber(instance.client)
                stub.add_response('converse',{'output':{'message':{'role':'assistant','content':[{'toolUse':{'toolUseId':'fixture-tool','name':'get_confirmed_preferences','input':{}}}]}},'stopReason':'tool_use','usage':{'inputTokens':10,'outputTokens':5,'totalTokens':15},'metrics':{'latencyMs':1}})
                proposal={'reply':'No confirmed preferences exist yet.','action':'help','amount':None,'task':None}
                stub.add_response('converse',{'output':{'message':{'role':'assistant','content':[{'text':json.dumps(proposal)}]}},'stopReason':'end_turn','usage':{'inputTokens':15,'outputTokens':10,'totalTokens':25},'metrics':{'latencyMs':1}})
                stub.activate();stubs.append(stub);return instance
            with patch.dict('os.environ',env),patch('strands.models.BedrockModel',side_effect=model):
                result,trace=BedrockIntake(work).complete([{'role':'system','content':'Return JSON.'},{'role':'user','content':'Use my previous preferences.'}])
            self.assertEqual(result['action'],'help');self.assertEqual(trace['model_calls'],2)
            self.assertEqual(trace['tools'],['get_confirmed_preferences'])
            for stub in stubs:stub.assert_no_pending_responses();stub.deactivate()

    def test_explicit_scp_error_is_sanitized(self):
        from botocore.exceptions import ClientError
        with tempfile.TemporaryDirectory() as tmp:
            env={'AWS_REGION':'us-east-1','SKEW_BEDROCK_MODEL_ID':'amazon.nova-lite-v1:0','SKEW_BEDROCK_ENABLED':'1','AWS_BEARER_TOKEN_BEDROCK':'fixture-token','AWS_EC2_METADATA_DISABLED':'true','SKEW_BEDROCK_ACCESS_STATUS':''}
            failure=ClientError({'Error':{'Code':'AccessDeniedException','Message':'explicit deny in a service control policy: arn:private-account'}},'Converse')
            with patch.dict('os.environ',env),patch('strands.models.BedrockModel',side_effect=failure),self.assertRaisesRegex(MachineError,'^ASSISTANT_BEDROCK_ORGANIZATION_DENY$'):
                BedrockIntake(Workspace(Path(tmp)/'work.db')).complete([{'role':'user','content':'hello'}])
