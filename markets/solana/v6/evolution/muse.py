"""Frozen Muse proposes bounded programs and an editable search policy."""
import asyncio,hashlib,json,stat,time
from pathlib import Path
import aiohttp
from schema import canonical,validate_program,validate_meta
MODEL='meta/muse-spark-1.3-contributor'
SYSTEM='''Invent a reusable market-making strategy graph for a controlled development market. Return only JSON with program and meta. Model weights are frozen. You may change features, conditional logic and graph wiring, not just constants. You cannot change the evaluator, owner limits, model, budget, permissions or execution code.
program={nodes:[[op,a,b,c,value],...],outputs:[skew,spread,clip,action]}. Outputs are node indices, all numeric. Maximum 40 nodes. Each reference must point to an earlier node. Unused a,b,c,value MUST be 0. No floats. op 1 feature(value index),2 constant(value),3 add(a,b),4 subtract(a,b),5 multiply(a,b),6 min(a,b),7 max(a,b),8 abs(a),9 negate(a),10 greater(a,b) boolean,11 less(a,b) boolean,12 and(a,b),13 or(a,b),14 not(a),15 select(boolean a,b,c). Feature indices:0 inventory[-8,8],1 imbalance[-100,100],2 momentum[-100,100],3 recent price range[0,100],4 book depth[0,4096],5 oracle age[0,100]. Boolean operators take booleans, select branches have matching types; outputs cannot be boolean. Every possible intermediate must stay within [-10000,10000]. Clamp with min/max. No division or arbitrary source code.
skew shifts quote center in ticks [-24,24]; spread is half-spread ticks [0,32], clipped to user minimum; clip is lots [0,2]; action=0 quote,1 reduce inventory only,2 pause. User position/clip/spread/risk limits override all outputs. No-fill idling fails evaluation. Costs, delayed markouts, drawdown and inventory are penalized. Shadow fill estimates are not proof of profitability.
meta={parent_mode:champion|diverse|recent,focus:[one to three of inventory,adverse_selection,momentum,liquidity,drawdown,churn],lesson:string at most 600 UTF8 bytes}. This changes the next search, not the evaluator. Propose one structurally new valid graph informed by the parent, past research outcomes and requested style. Return no prose outside JSON.'''

def unique(pairs):
    d={}
    for k,v in pairs:
        if k in d:raise ValueError('duplicate JSON key')
        d[k]=v
    return d

def request(parent,p,feedback,meta):
    # Explicit projection: no profile hash/user ID, wallet, account address,
    # raw receipt, credentials or customer record can enter the request.
    public={'development_style':p['style'],'objective_weights':p['weights'],'generic_limits':p['limits'],
        'parent_program':parent,'public_research_feedback':feedback,'search_policy':meta}
    return {'model':MODEL,'reasoning':{'effort':'minimal','exclude':True},'max_tokens':4096,'stream':False,
        'provider':{'only':['meta'],'allow_fallbacks':False,'require_parameters':True,'data_collection':'allow','max_price':{'prompt':0.10,'completion':0.20}},
        'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':canonical(public)}],
        'response_format':{'type':'json_object'}}

def parse(value):
    if value.get('model')!=MODEL or value.get('provider') not in ('meta','Meta'):raise ValueError('model/provider route')
    choices=value.get('choices')
    if type(choices) is not list or len(choices)!=1 or choices[0].get('finish_reason')!='stop':raise ValueError('incomplete response')
    m=choices[0].get('message',{});s=m.get('content')
    if m.get('refusal') or m.get('tool_calls') or type(s) is not str or len(s.encode())>12000:raise ValueError('response shape')
    out=json.loads(s,object_pairs_hook=unique)
    if type(out) is not dict or set(out)!={'program','meta'}:raise ValueError('proposal shape')
    validate_program(out['program']);validate_meta(out['meta']);return out

async def call(req,keypath):
    raw=canonical(req).encode()
    if len(raw)>16000:raise ValueError('input byte budget')
    if stat.S_IMODE(keypath.stat().st_mode)&0o077:raise ValueError('credential file permissions')
    start=time.monotonic();response_value=None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=55,connect=5)) as client:
        async with client.post('https://openrouter.ai/api/v1/chat/completions',data=raw,
            headers={'Content-Type':'application/json','Authorization':'Bearer '+keypath.read_text().strip()},allow_redirects=False) as response:
            if response.status!=200:raise ValueError('provider HTTP '+str(response.status))
            body=bytearray()
            async for chunk in response.content.iter_chunked(4096):
                body.extend(chunk)
                if len(body)>65536:raise ValueError('response byte budget')
            response_value=json.loads(body,object_pairs_hook=unique)
    record={'model':response_value.get('model'),'provider':response_value.get('provider'),'usage':response_value.get('usage'),
        'choices':response_value.get('choices'),
        'latency_ms':round((time.monotonic()-start)*1000,3),'response_hash':hashlib.sha256(body).hexdigest()}
    try:record.update(accepted=True,proposal=parse(response_value))
    except ValueError as e:record.update(accepted=False,error=str(e)[:160])
    return record
