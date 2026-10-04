"""Stateless MCP 2025-11-25; tools propose purchases but never approve them."""
import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from economic_machine.values import MachineError, require_keys

VERSION = '2025-11-25'

def schema(properties, required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}

STRING={'type':'string','minLength':1,'maxLength':100}
BRIEF=schema({
    'request_id':STRING,'kind':{'type':'string','enum':['vendor_comparison','research_brief','document_draft','data_cleanup','content_localization']},
    'title':{'type':'string','maxLength':160},'instructions':{'type':'string','maxLength':12000},
    'budget':schema({'currency':{'const':'USD'},'maximum':{'type':'string','pattern':r'^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$'}}),
    'deadline_at':{'type':['integer','null']},'constraints':{'type':'object','description':'Explicit conditions only: regions, output_language, output_format, required_fields, comparison_fields, tone, source_language or excluded_providers.'},
    'preference_id':{'type':['string','null'],'description':'Use last only when the user requests confirmed prior conditions.'},
    'connection_ids':{'type':'array','items':STRING,'maxItems':16}})
TOOLS = [
    ('create_task','Save a typed task. A budget is a ceiling, never payment consent.',schema({'brief':BRIEF}),False),
    ('get_confirmed_preferences','Read owner-confirmed preferences. Never inherit spending approval.',schema({}),True),
    ('compare_plans','Compare eligible configured services for a saved task, without buying.',schema({'task_id':STRING}),True),
    ('request_purchase_approval','Freeze a purchase proposal and return the owner review link. Does not charge.',schema({'task_id':STRING,'request_id':STRING,'expected_revision':{'type':'integer','minimum':1},'sku':STRING,'input':{'type':'object'}}),False),
    ('get_task_status','Read this workspace task and its purchase progress.',schema({'task_id':STRING}),True),
    ('get_deliverable','Return a paid result only with explicit data-read scope.',schema({'purchase_id':STRING}),True),
    ('request_cancel','Return an owner cancellation review. Does not cancel or refund money.',schema({'purchase_id':STRING}),True),
]

def compare_plans(work, tid):
    task=work.tasks.get(tid)
    status=work.task_checkout.status()
    options=[]
    if task['status']=='READY_TO_PLAN' and status['configured']:
        for item in status['services']:
            if item['kind']==task['brief']['kind'] and item['price_cents']<=task['brief']['budget']['cents']:
                options.append({**item,'merchant_id':work.task_checkout.provider.merchant,
                    'currency':'USD','environment':'PAYPAL_SANDBOX','requires_owner_approval':True})
    return {'task_id':tid,'revision':task['revision'],'brief_hash':task['brief_hash'],
        'options':sorted(options,key=lambda x:(x['price_cents'],x['sku'])),
        'status':'READY' if options else 'NO_ELIGIBLE_CONFIGURED_SERVICE',
        'missing_information':task['missing_information'],'charged':False}


def dispatch(work, name, args, principal=None):
    definitions={item[0]:item for item in TOOLS}
    if name not in definitions:raise MachineError('MCP_TOOL_NOT_SUPPORTED')
    definition=definitions[name]
    require_keys(args,set(definition[2]['required']),'MCP tool arguments')
    owner=principal is None or principal.owner
    if not owner:
        needed='data:read' if name=='get_deliverable' else 'engine:read' if definition[3] else 'engine:write'
        if needed not in principal.scopes:raise MachineError('MCP_TOOL_SCOPE_REQUIRED')
    if name=='create_task':return work.tasks.create(args['brief'])
    if name=='get_confirmed_preferences':return {'preferences':[p for p in work.tasks.status()['preferences'] if p['status']=='CONFIRMED']}
    if name=='compare_plans':return compare_plans(work,args['task_id'])
    if name=='request_purchase_approval':
        raw={k:v for k,v in args.items() if k!='task_id'}
        purchase=work.task_checkout.plan(args['task_id'],raw)
        return {'purchase':purchase,'owner_review_url':'/commerce/console#tasks','payment_authority':'NONE'}
    if name=='get_task_status':
        task=work.tasks.get(args['task_id'])
        return {'task':task,'purchases':[p for p in work.task_checkout.status()['purchases'] if p['task_id']==task['id']]}
    if name=='get_deliverable':return work.task_checkout.delivery(args['purchase_id'])
    purchase=work.task_checkout.get(args['purchase_id'])
    return {'purchase_id':purchase['id'],'status':purchase['status'],'owner_review_url':'/commerce/console#tasks','cancelled':False}


def mcp_routes(workspace_dependency, *, origin=None):
    router=APIRouter()
    def boundary(request):
        supplied=request.headers.get('origin')
        expected=origin or str(request.base_url).rstrip('/')
        if supplied and supplied!=expected:return JSONResponse({'error':'MCP_ORIGIN_REJECTED'},status_code=403)
        return None

    @router.api_route('/mcp',methods=['GET','DELETE'])
    def no_stream(request:Request,work=Depends(workspace_dependency)):
        return boundary(request) or Response(status_code=405,headers={'Allow':'POST'})

    @router.post('/mcp')
    async def post(request:Request,work=Depends(workspace_dependency)):
        refused=boundary(request)
        if refused is not None:return refused
        if not all(x in request.headers.get('accept','') for x in ('application/json','text/event-stream')):
            return JSONResponse({'error':'MCP_ACCEPT_REQUIRED'},status_code=406)
        if len(await request.body())>48000:return Response(status_code=413)
        try:raw=await request.json()
        except ValueError:return JSONResponse({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}},status_code=400)
        ident=raw.get('id') if isinstance(raw,dict) else None
        def error(code,message,status=200):
            return JSONResponse({'jsonrpc':'2.0','id':ident,'error':{'code':code,'message':message}},status_code=status)
        if (not isinstance(raw,dict) or raw.get('jsonrpc')!='2.0' or not isinstance(raw.get('method'),str)
                or set(raw)-{'jsonrpc','id','method','params'} or ('id' in raw and (type(ident) not in (int,str)))):
            return error(-32600,'Invalid Request',400)
        method,params=raw['method'],raw.get('params',{})
        if not isinstance(params,dict):return error(-32602,'Invalid params')
        if method!='initialize' and request.headers.get('MCP-Protocol-Version')!=VERSION:
            return error(-32600,'Unsupported protocol version',400)
        if 'id' not in raw:
            return Response(status_code=202) if method in {'notifications/initialized','notifications/cancelled'} else error(-32600,'Unsupported notification',400)
        if method=='initialize':
            if not isinstance(params.get('protocolVersion'),str) or not isinstance(params.get('capabilities'),dict) or not isinstance(params.get('clientInfo'),dict):
                return error(-32602,'Initialization fields required')
            result={'protocolVersion':VERSION,'capabilities':{'tools':{'listChanged':False}},
                'serverInfo':{'name':'skew-economic-machine','version':'1.0.0'},
                'instructions':'Task tools never grant payment consent. Review exact purchases in the owner console.'}
        elif method=='ping':result={}
        elif method=='tools/list':
            result={'tools':[{'name':n,'description':d,'inputSchema':s,'annotations':{'readOnlyHint':r,'destructiveHint':False,'openWorldHint':False}} for n,d,s,r in TOOLS]}
        elif method=='tools/call':
            if not isinstance(params.get('name'),str) or not isinstance(params.get('arguments',{}),dict):return error(-32602,'Tool name and object arguments required')
            import asyncio
            try:
                value=await asyncio.to_thread(dispatch,work,params['name'],params.get('arguments',{}),getattr(request.state,'principal',None))
                result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],'structuredContent':value,'isError':False}
            except (MachineError,TypeError,KeyError,ValueError) as exc:
                message=str(exc) if isinstance(exc,MachineError) else 'MCP_INVALID_TOOL_ARGUMENTS'
                result={'content':[{'type':'text','text':message}],'isError':True}
        else:return error(-32601,'Method not found')
        return JSONResponse({'jsonrpc':'2.0','id':ident,'result':result},headers={'MCP-Protocol-Version':VERSION,'Cache-Control':'no-store'})
    return router
