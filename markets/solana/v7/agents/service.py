"""Independent bounded processes: feed, makers, flow, oracle keeper, Muse policy."""
import argparse,asyncio,fcntl,json,os,struct,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'muse'))
from devnet import BASE,GENESIS
from wire import *
from solders.keypair import Keypair
from native import Native
from rt_net import Net,Cache,publish_feed
from rt_store import Store
from rt_tx import Tx
from controller import control
from topology import is_maker,keeper,validate

def policy(private):
    value=json.loads((private/'policy.json').read_text())
    if value.get('execution_authority') is not False or type(value.get('code')) is not int or value['code'] not in range(4):raise RuntimeError('invalid policy record')
    if value['code']<2 or value['until_unix']<time.time()+3:raise RuntimeError('policy stopped or expired')
    return value

def market_ok(st,slot,manifest):
    if st['halted'] or st['oracle_mode']!=1 or not 0<=int(time.time())-st['oracle_publish_time']<=90 or not 0<=slot-st['oracle_slot']<=100:raise RuntimeError('oracle halted or stale')
    validate(manifest)
    if len(st['seats'])!=len(manifest['agents']) or [s['owner'] for s in st['seats']]!=manifest['agents'] or [s['seat'] for s in st['seats']]!=list(range(len(manifest['agents']))):raise RuntimeError('unexpected market actors')
    for i,s in enumerate(st['seats']):
        if abs(s['position'])>(8 if is_maker(i,manifest) else 16) or min(s['equity'],s['collateral'])<500000:raise RuntimeError('risk reserve')

async def actor(role,folder,private,manifest,mandate,store,cache):
    key=Keypair.from_json((private/f'session-{role}.json').read_text())
    engine=Native(role,time.monotonic_ns()+int(max(1,mandate['ends_unix']-time.time()))*1_000_000_000) if role<2 else None
    next_action=0;last_quote=None;last_error=None
    async with Net() as net:
        if await net.call('getGenesisHash',[])!=GENESIS:raise RuntimeError('wrong chain')
        tx=Tx(role,key,manifest,mandate,net,store)
        await tx.recover(True);store.event(role,'actor_started',{'pid':os.getpid()})
        while time.time()<mandate['ends_unix'] and not (private/'stop').exists():
            try:
                if store.pending(role):
                    await tx.recover();last_quote=None
                    if store.pending(role):await asyncio.sleep(1);continue
                if len(store.rows(role))>=mandate['actor_transaction_limit']-1:break
                st,slot,received,generation=cache.read()
                if role==keeper(manifest):
                    if time.time()<next_action:await asyncio.sleep(0.25);continue
                    op=17 if st['funding_observed_seconds']>=240 and time.time()-st['funding_window_start']>=300 else 16
                    await tx.send(st,op,source_ns=received,kind='funding' if op==17 else 'pyth-refresh');next_action=time.time()+8;continue
                market_ok(st,slot,manifest)
                candidate=policy(private)
                if st['seats'][role]['session_turnover_remaining']<st['oracle']:
                    store.event(role,'turnover_exhausted',{});break
                if role<2:
                    now=time.monotonic_ns();until=now+int((candidate['until_unix']-time.time())*1e9)
                    engine.policy(candidate['code'],until,now);decision=engine.decide(st,slot,received);decided=time.monotonic_ns()
                    # Preserve exact facts and decision before acknowledging replay compaction.
                    store.event(role,'decision',{'source_slot':slot,'source_generation':generation,'decision':decision,'policy':candidate})
                    engine.checkpoint(decision)
                    qs=decision['quotes'];fingerprint=[(q['slot'],q['price'],q['lots'],q['reduce']) for q in qs]
                    own=[o for o in st['orders'] if o['seat']==role]
                    renew=not own or min(o['policy_until'] for o in own)<time.time()+10
                    changed=last_quote!=fingerprint or sum(o['lots'] for o in own)!=sum(q['lots'] for q in qs)
                    if not changed and not renew:await asyncio.sleep(0.3);continue
                    if time.time()<next_action:await asyncio.sleep(0.15);continue
                    expiry=min(slot+45,st['seats'][role]['session_expiry'])
                    frame=quotes([(q['slot'],q['price'],q['lots'],expiry,len(store.rows(role))*16+q['slot'],q['reduce']) for q in qs],int(candidate['until_unix']))
                    result=await tx.send(st,5,frame,policy_until=candidate['until_unix'],source_ns=received,decision_ns=decided,kind='quote')
                    last_quote=fingerprint if result and result['state']=='CONFIRMED' else None;next_action=time.time()+1.5
                else:
                    if time.time()<next_action:await asyncio.sleep(0.2);continue
                    q=st['seats'][2]['position'];count=sum(json.loads(r['body'])['op']==6 for r in store.rows(role))
                    side=1 if q>=6 else 0 if q<=-6 else (0 if (count//3)%2==0 else 1)
                    live=[o for o in st['orders'] if o['seat']!=role and o['side']!=side and o['expiry']>=slot and (not o['policy_until'] or o['policy_until']>time.time()+2)]
                    if not live:await asyncio.sleep(0.5);continue
                    limit=min(o['price'] for o in live) if side==0 else max(o['price'] for o in live)
                    if abs(q+(2 if side==0 else -2))>16:raise RuntimeError('flow position cap')
                    await tx.send(st,6,ioc(side,2,limit,minimum=0,budget=2*limit,visits=16,policy_until=int(candidate['until_unix'])),policy_until=candidate['until_unix'],source_ns=received,kind='synthetic-demand')
                    next_action=time.time()+4
                last_error=None
            except (RuntimeError,ValueError,OSError,KeyError) as error:
                reason=str(error) if isinstance(error,RuntimeError) else type(error).__name__
                if reason!=last_error:store.event(role,'actor_paused',{'reason':reason});last_error=reason
                if role<2 and not store.pending(role):
                    try:
                        current,_,source=await tx.current()
                        if any(o['seat']==role for o in current['orders']):
                            await tx.send(current,7,struct.pack('<Q',65535),source_ns=source,kind='safety-cancel');last_quote=None
                    except RuntimeError:pass
                await asyncio.sleep(0.5)
        # Pending commands are always reconciled before any final cancel.
        try:
            await tx.recover(True)
            if role<3 and time.time()<mandate['ends_unix']:
                st,_,received=await tx.current()
                if any(o['seat']==role for o in st['orders']):await tx.send(st,7,struct.pack('<Q',65535),source_ns=received,kind='stop-cancel')
        except RuntimeError as error:store.event(role,'shutdown_pending',{'reason':str(error)})
    if engine:engine.close()

async def main(run,role):
    private=BASE/'private'/run;folder=BASE/'evidence'/run
    mandate=json.loads((private/'operating-mandate.json').read_text());manifest=json.loads((folder/'manifest.json').read_text())
    lock=(private/f'role-{role}.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store=Store(private/'runtime.sqlite',mandate)
    if role==-1:return await publish_feed(folder,private,manifest,mandate,store)
    while not (private/'market.cache').exists():await asyncio.sleep(0.1)
    cache=Cache(private/'market.cache')
    try:
        if role==4:await control(folder,private,manifest,mandate,store,cache)
        else:await actor(role,folder,private,manifest,mandate,store,cache)
    finally:cache.close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--role',type=int,choices=range(-1,5),required=True);a=p.parse_args()
    if not a.run.startswith('devnet-agents-') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in a.run):raise SystemExit('run name')
    asyncio.run(main(a.run,a.role))
