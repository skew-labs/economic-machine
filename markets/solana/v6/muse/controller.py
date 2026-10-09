"""Bounded public-devnet regime controller. No keys/positions/addresses in prompts."""
import asyncio,fcntl,hashlib,json,stat,sys,time
from pathlib import Path
import aiohttp
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agents'))
from loop import save
from contract import ActivationState,NOTICE_VERSION,DATA_SCOPE
from policy_catalog import validate_compact
MODEL='meta/muse-spark-1.3-contributor'
MAKER_SYSTEM='Classify this synthetic maker book. Return only {"c":N}. N=0 for malformed features; N=3 when d[0]=0 or abs(i)>=5000 or v>=30; N=1 when s>200; otherwise N=2. Empty depth is a maker bootstrap state: conservative quotes (3) may create liquidity. s=spread ticks,i=imbalance bps,d=public depth lots. v=0 is a fixed omitted external-volatility feature. Native risk checks independently authorize every order. No explanation.'
async def bounded_response(content):
    body=bytearray()
    async for chunk in content.iter_chunked(4096):
        body.extend(chunk)
        if len(body)>65536:raise RuntimeError('response byte cap')
    return json.loads(body)
def public_features(st):
    now=int(time.time());orders=[o for o in st['orders'] if o['expiry']>=st['read_slot'] and (not o['policy_until'] or o['policy_until']>now)]
    bids=sorted([o for o in orders if o['side']==0],key=lambda o:-o['price']);asks=sorted([o for o in orders if o['side']==1],key=lambda o:o['price'])
    buy=sum(o['lots'] for o in bids);sell=sum(o['lots'] for o in asks);total=buy+sell
    spread=asks[0]['price']-bids[0]['price'] if bids and asks else 0
    # Only aggregates of the owner's publicly posted development orders. Neither
    # oracle prices nor account inventory, addresses or private mandates are sent.
    return {'s':max(0,spread),'i':int((buy-sell)*10000//total) if total else 0,'v':0,
            'd':[sum(o['lots'] for o in bids[:n])+sum(o['lots'] for o in asks[:n]) for n in (1,4,16)]}
def request(features):
    if set(features)!={'s','i','v','d'} or any(type(x) is not int for x in [features['s'],features['i'],features['v'],*features['d']]):raise ValueError('public projection only')
    return {'model':MODEL,'reasoning':{'effort':'minimal','exclude':True},'max_tokens':256,'stream':False,
      'provider':{'only':['meta'],'allow_fallbacks':False,'require_parameters':True,'data_collection':'allow','max_price':{'prompt':0.10,'completion':0.20}},
      'messages':[{'role':'system','content':MAKER_SYSTEM},
                  {'role':'user','content':json.dumps(features,separators=(',',':'))}],
      'response_format':{'type':'json_schema','json_schema':{'name':'regime_v2','strict':True,'schema':{'type':'object','properties':{'c':{'type':'integer','enum':[0,1,2,3]}},'required':['c'],'additionalProperties':False}}}}
async def control(folder,private,manifest,mandate,store,cache):
    lock=(private/'muse.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    keypath=private.parent/'openrouter.key'
    if stat.S_IMODE(keypath.stat().st_mode)&0o077:raise RuntimeError('API key file must be private')
    key=keypath.read_text().strip();next_at=max(time.time(),mandate['starts_unix']+6)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25,connect=5)) as client:
        while time.time()<mandate['ends_unix'] and not (private/'stop').exists():
            if time.time()<next_at:await asyncio.sleep(min(0.5,next_at-time.time()));continue
            if mandate['fault_drill'] and 150<=time.time()-mandate['starts_unix']<195:
                if not store.count_events(4,'policy_expiry_injected'):store.event(4,'policy_expiry_injected',{})
                await asyncio.sleep(1);continue
            grant=json.loads((private/'activation.json').read_text())
            if not grant.get('enabled') or grant.get('revoked') or time.time()>=grant['expires_unix']:raise RuntimeError('activation revoked or expired')
            count=store.count_events(4,'muse_reserved')
            if count>=mandate['max_muse_calls']:store.event(4,'muse_budget_exhausted',{});return
            try:st,_,_,_=cache.read()
            except RuntimeError:await asyncio.sleep(1);continue
            features=public_features(st);req=request(features);raw=json.dumps(req,separators=(',',':')).encode()
            if len(raw)>2048:raise RuntimeError('public request byte cap')
            start=time.monotonic_ns();wall=time.time();index=count+1
            activation=ActivationState(enabled=True,training_acknowledged=grant['training_acknowledged'],notice_version=grant['notice_version'],data_scope=grant['data_scope'],eligible=True,source_use_approved=True,expires_ns=start+int(grant['expires_unix']-wall)*1_000_000_000)
            store.event(4,'muse_reserved',{'index':index,'request_hash':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'maximum_output_tokens':256})
            save(folder/f'muse-request-{index:03d}.json',req);next_at=wall+20
            record={'index':index,'source':'owner-created public devnet orderbook aggregates','started_unix':wall,'model':MODEL,'features':features}
            try:
                async with client.post('https://openrouter.ai/api/v1/chat/completions',data=raw,headers={'Content-Type':'application/json','Authorization':'Bearer '+key},allow_redirects=False) as response:
                    if response.status!=200:raise RuntimeError(f'provider HTTP {response.status}')
                    parsed=await bounded_response(response.content)
                record.update(usage=parsed.get('usage'),finish_reasons=[c.get('finish_reason') for c in parsed.get('choices',[])])
                ended=time.monotonic_ns();candidate=validate_compact(parsed,started_ns=start,now_ns=ended,activation=activation)
                # Recheck the persistent grant immediately before activation.
                latest=json.loads((private/'activation.json').read_text())
                if latest!=grant:raise RuntimeError('activation changed during request')
                policy={'code':candidate['code'],'until_unix':wall+30,'request_index':index,'source':'muse-live','model':MODEL,'execution_authority':False}
                save(private/'policy.json',policy)
                record.update(accepted=True,policy=policy,usage=parsed.get('usage'),latency_ms=(ended-start)/1e6)
                store.event(4,'muse_accepted',record)
            except (Exception,) as error:
                record.update(accepted=False,error_type=type(error).__name__,error=str(error)[:180],latency_ms=(time.monotonic_ns()-start)/1e6)
                save(private/'policy.json',{'code':0,'until_unix':time.time()+2,'source':'provider-failure','execution_authority':False})
                store.event(4,'muse_rejected',record)
            save(folder/f'muse-result-{index:03d}.json',record)
