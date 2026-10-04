import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from economic_machine.values import MachineError
from machine_engine.api import create_engine_app
from machine_engine.workspace import Workspace
from machine_engine.task_mcp import dispatch, VERSION
from machine_commerce.api import create_app
from machine_commerce.access import Principal
from test_engine_workspace import OWNER_TOKEN
from test_tasks import brief

HEADERS={'Authorization':'Bearer '+OWNER_TOKEN,'Accept':'application/json, text/event-stream','MCP-Protocol-Version':VERSION}
class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.work=Workspace(Path(self.tmp.name)/'work.db')
        self.client=TestClient(create_engine_app(self.work.db_path if hasattr(self.work,'db_path') else Path(self.tmp.name)/'work.db',admin_token=OWNER_TOKEN,workspace=self.work))
    def tearDown(self):self.client.close();self.tmp.cleanup()
    def call(self,method,params=None,**extras):
        return self.client.post('/api/engine/mcp',headers=HEADERS,json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}|extras)
    def test_version_discovery_and_stateless_resume(self):
        r=self.call('initialize',{'protocolVersion':VERSION,'capabilities':{},'clientInfo':{'name':'test','version':'1'}})
        self.assertEqual(r.json()['result']['protocolVersion'],VERSION)
        tools=self.call('tools/list').json()['result']['tools'];self.assertEqual(len(tools),7)
        task=self.call('tools/call',{'name':'create_task','arguments':{'brief':brief()}}).json()['result']['structuredContent']
        other=TestClient(create_engine_app(Path(self.tmp.name)/'work.db',admin_token=OWNER_TOKEN))
        result=other.post('/api/engine/mcp',headers=HEADERS,json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_task_status','arguments':{'task_id':task['id']}}})
        self.assertEqual(result.json()['result']['structuredContent']['task']['id'],task['id']);other.close()
    def test_http_auth_origin_protocol_and_notification(self):
        self.assertEqual(self.client.post('/api/engine/mcp',json={}).status_code,401)
        self.assertEqual(self.client.get('/api/engine/mcp',headers=HEADERS|{'Origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.client.get('/api/engine/mcp',headers=HEADERS).status_code,405)
        self.assertEqual(self.client.post('/api/engine/mcp',headers=HEADERS|{'MCP-Protocol-Version':'invalid'},json={'jsonrpc':'2.0','id':1,'method':'ping'}).status_code,400)
        self.assertEqual(self.client.post('/api/engine/mcp',headers=HEADERS,json=[{}]).status_code,400)
        r=self.client.post('/api/engine/mcp',headers=HEADERS,json={'jsonrpc':'2.0','method':'notifications/initialized'})
        self.assertEqual(r.status_code,202);self.assertEqual(r.content,b'')
    def test_agent_cannot_self_approve_or_escape_workspace(self):
        readonly=Principal('test','key',frozenset({'engine:read'}))
        with self.assertRaisesRegex(MachineError,'SCOPE'):dispatch(self.work,'create_task',{'brief':brief()},readonly)
        with self.assertRaisesRegex(MachineError,'NOT_SUPPORTED'):dispatch(self.work,'capture',{},readonly)
        task=dispatch(self.work,'create_task',{'brief':brief()},Principal('test','key',frozenset({'engine:read','engine:write'})))
        foreign=Workspace(Path(self.tmp.name)/'foreign.db')
        with self.assertRaisesRegex(MachineError,'TASK_NOT_FOUND'):dispatch(foreign,'get_task_status',{'task_id':task['id']})
        with self.assertRaisesRegex(MachineError,'SCOPE'):dispatch(self.work,'get_deliverable',{'purchase_id':'missing'},readonly)
    def test_no_invented_offer_without_admitted_provider(self):
        task=self.work.tasks.create(brief())
        result=dispatch(self.work,'compare_plans',{'task_id':task['id']})
        self.assertEqual(result['options'],[]);self.assertFalse(result['charged'])
    def test_hosted_endpoint_requires_scope_and_isolates_owner(self):
        client=TestClient(create_app(Path(self.tmp.name)/'commerce.db'))
        client.post('/api/sessions',json={})
        owner={}
        key=client.post('/api/keys',headers=owner,json={'name':'MCP','scopes':['engine:read'],'policy_id':None,'ttl_seconds':600}).json()
        credential=key['secret']
        self.assertIsInstance(credential,str)
        r=client.post('/api/engine/mcp',headers=HEADERS|{'Authorization':'Bearer '+credential},json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'create_task','arguments':{'brief':brief()}}})
        self.assertTrue(r.json()['result']['isError'])
        self.assertIn('SCOPE',r.json()['result']['content'][0]['text']);client.close()

class MCPPaymentBoundaryTests(unittest.TestCase):
    def test_all_payment_tools_stop_at_owner_review(self):
        import test_task_checkout as fixture
        c=fixture.CheckoutTests();c.setUp()
        try:
            writer=Principal('test','key',frozenset({'engine:read','engine:write','data:read'}))
            task=c.work.task_checkout.get(c.pid)['task_id']
            plans=dispatch(c.work,'compare_plans',{'task_id':task},writer)
            self.assertEqual(len(plans['options']),1)
            proposed=dispatch(c.work,'request_purchase_approval',{'task_id':task,**c.raw},writer)
            self.assertEqual(proposed['purchase']['status'],'PLANNED');self.assertEqual(c.paypal.creates,[])
            result=dispatch(c.work,'request_cancel',{'purchase_id':c.pid},writer)
            self.assertFalse(result['cancelled']);self.assertEqual(c.work.task_checkout.get(c.pid)['status'],'PLANNED')
            with self.assertRaises(MachineError):dispatch(c.work,'get_deliverable',{'purchase_id':c.pid},writer)
            c.pay();c.work.task_checkout.fulfill(c.pid)
            delivered=dispatch(c.work,'get_deliverable',{'purchase_id':c.pid},writer)
            self.assertTrue(delivered['content']);self.assertEqual(len(c.paypal.captures),1)
        finally:c.tearDown()
