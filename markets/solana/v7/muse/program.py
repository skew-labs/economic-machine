"""Muse proposes a bounded conditional program, never a per-tick trading signal."""
import hashlib,json,time
from pathlib import Path
from contract import Rejected,unique_object
MODEL='meta/muse-spark-1.3-contributor'

SYSTEM='Propose a reusable maker program, not a current-market decision. Return only {"p":[b,w,i,j,r,c]}. b=normal half-spread ticks (4..12), w=stressed half-spread (b..32), i=absolute book imbalance threshold bps (2000..8000), j=one-event mark movement threshold bps (5..100), r=inventory reduction threshold lots (2..4), c=normal clip lots (1..2). Runtime: empty book OR imbalance>=i OR movement>=j means stressed mode (one level, clip 1); otherwise normal mode (two levels, clip c). abs(inventory)>=r means reduce-only. Stale/invalid facts or revoked owner authority stop execution independently. Choose a balanced conservative development configuration; no explanations.'
LIMITS=((4,12),(4,32),(2000,8000),(5,100),(2,4),(1,2))
def parameters(value):
    if type(value) is not dict or set(value)!={'p'} or type(value['p']) is not list or len(value['p'])!=6:raise Rejected('PROGRAM_SHAPE')
    p=value['p']
    if any(type(x) is not int or not lo<=x<=hi for x,(lo,hi) in zip(p,LIMITS)) or p[1]<p[0]:raise Rejected('PROGRAM_BOUNDS')
    return p
def content_hash(params):
    return hashlib.sha256(json.dumps({'compiler':'mp-conditional-v3','parameters':params},sort_keys=True,separators=(',',':')).encode()).hexdigest()
def request():
    return {'model':MODEL,'reasoning':{'effort':'minimal','exclude':True},'max_tokens':1024,'stream':False,
      'provider':{'only':['meta'],'allow_fallbacks':False,'require_parameters':True,'data_collection':'allow','max_price':{'prompt':0.10,'completion':0.20}},
      'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':'Compile one reusable SOL development maker policy under the given public bounds.'}],
      'response_format':{'type':'json_schema','json_schema':{'name':'conditional_maker_v3','strict':True,'schema':{'type':'object','properties':{'p':{'type':'array','items':{'type':'integer'},'minItems':6,'maxItems':6}},'required':['p'],'additionalProperties':False}}}}
def validate(response,elapsed_seconds):
    if not 0<=elapsed_seconds<30:raise Rejected('COMPILATION_DEADLINE')
    if response.get('model')!=MODEL or response.get('provider') not in ('meta','Meta'):raise Rejected('ROUTE_ID')
    choices=response.get('choices')
    if type(choices) is not list or len(choices)!=1 or choices[0].get('finish_reason')!='stop':raise Rejected('INCOMPLETE')
    m=choices[0].get('message',{})
    if m.get('refusal') or m.get('tool_calls'):raise Rejected('NON_PROGRAM_RESPONSE')
    text=m.get('content')
    if type(text) is not str or len(text.encode())>256:raise Rejected('OUTPUT_SIZE')
    p=parameters(json.loads(text,object_pairs_hook=unique_object))
    return {'compiler':'mp-conditional-v3','parameters':p,'program_hash':content_hash(p),'model':MODEL,'execution_authority':False}

async def propose(folder,private):
    """At most three calls across recovery. No wallet/account/feed data in request."""
    import aiohttp,stat
    from controller import bounded_response
    from loop import save,BASE
    accepted=folder/'compiled-proposal.json'
    if accepted.exists():
        p=json.loads(accepted.read_text());assert p['program_hash']==content_hash(parameters({'p':p['parameters']}));return p
    keypath=BASE/'private/openrouter.key'
    if stat.S_IMODE(keypath.stat().st_mode)&0o077:raise RuntimeError('API key must be private')
    req=request();raw=json.dumps(req,separators=(',',':')).encode();assert len(raw)<=2048
    budget=private/'program-call-reservations.json';reservations=json.loads(budget.read_text()) if budget.exists() else []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25,connect=5)) as client:
        while len(reservations)<3:
            index=len(reservations)+1;start=time.monotonic();wall=time.time()
            reservations.append({'index':index,'started_unix':wall,'request_hash':hashlib.sha256(raw).hexdigest(),'max_output_tokens':1024})
            save(budget,reservations);save(folder/f'program-request-{index}.json',req)
            record={'index':index,'started_unix':wall,'source':'public generic program specification only','maximum_output_tokens':1024}
            try:
                async with client.post('https://openrouter.ai/api/v1/chat/completions',data=raw,headers={'Content-Type':'application/json','Authorization':'Bearer '+keypath.read_text().strip()},allow_redirects=False) as response:
                    if response.status!=200:raise RuntimeError(f'provider HTTP {response.status}')
                    value=await bounded_response(response.content)
                elapsed=time.monotonic()-start;proposal=validate(value,elapsed)
                proposal.update(version=1,proposed_unix=wall,accepted_unix=time.time(),source='muse-live-program')
                record.update(accepted=True,latency_ms=elapsed*1000,usage=value.get('usage'),proposal=proposal)
                save(accepted,proposal);save(folder/f'program-result-{index}.json',record);return proposal
            except Exception as error:
                record.update(accepted=False,error_type=type(error).__name__,error=str(error)[:160],latency_ms=(time.monotonic()-start)*1000)
                save(folder/f'program-result-{index}.json',record)
    raise RuntimeError('bounded Muse compiler attempts exhausted; no market funded')
