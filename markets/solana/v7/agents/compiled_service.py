"""Market events evaluate installed bytecode while the independent dispatcher waits."""
import argparse,asyncio,fcntl,json,os,struct,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'muse'))
from devnet import BASE,GENESIS
from wire import *
from rt_net import Net,Cache,publish_feed
from rt_store import Store
from rt_tx import Tx
from compiled import Compiled
from service import market_ok,actor as old_actor
from solders.keypair import Keypair
from topology import is_maker,keeper,validate

def local_features(st,slot,previous_mark):
    now=time.time();orders=[o for o in st['orders'] if o['expiry']>=slot and (not o['policy_until'] or o['policy_until']>now)]
    buy=sum(o['lots'] for o in orders if o['side']==0);sell=sum(o['lots'] for o in orders if o['side']==1);total=buy+sell
    return {'imbalance':(buy-sell)*10000//total if total else 0,'depth':total,'movement':min(10000,abs(st['oracle']-previous_mark)*10000//previous_mark) if previous_mark else 0}

class Events:
    def __init__(self,role,private,manifest,mandate,store,cache,proposal):
        self.role=role;self.private=private;self.manifest=manifest;self.mandate=mandate;self.store=store;self.cache=cache;self.latest=None
        self.engine=Compiled(role,time.monotonic_ns()+int(max(0,mandate['ends_unix']-time.time())*1e9),proposal) if is_maker(role,manifest) else None
        if self.engine and role<6 and (private/'adaptive-grant.json').exists():
            sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adaptive'))
            from live import Adaptive
            self.engine=Adaptive(self.engine,role,private,mandate,store)
    def active(self):return time.time()<self.mandate['ends_unix'] and not (self.private/'stop').exists()
    def owner_gate(self):
        grant=json.loads((self.private/'activation.json').read_text())
        if not grant.get('enabled') or grant.get('revoked') or time.time()>=min(grant['expires_unix'],self.mandate['ends_unix']):raise RuntimeError('owner program lease revoked or expired')
    async def pump(self):
        generation=0;previous_mark=0;last_error=None;next_gate=0
        try:
            while self.active():
                try:
                    if time.monotonic()>=next_gate:self.owner_gate();next_gate=time.monotonic()+.05
                    compact=self.engine is not None and self.mandate.get('compact_feed',False)
                    changed=self.cache.read_prepared_if_changed(generation) if compact else self.cache.read_if_changed(generation)
                    if changed is None:await asyncio.sleep(.001 if compact else .005);continue
                    st,slot,received,generation=changed[:4];start=time.monotonic_ns()
                    if compact:decision,features,previous_mark=self.engine.decide_prepared(st,changed[4],changed[5],slot,received,previous_mark)
                    else:
                        market_ok(st,slot,self.manifest)
                        features=local_features(st,slot,previous_mark);previous_mark=st['oracle']
                        decision=self.engine.decide(st,slot,received,features) if self.engine else None
                    end=time.monotonic_ns()
                    if decision:
                        self.store.event(self.role,'compiled_decision',{'source_generation':generation,'source_slot':slot,'source_ns':received,'evaluation_start_ns':start,'evaluation_end_ns':end,'features':features,'decision':decision,'program_hash':decision.get('program_hash',self.mandate['program_hash']),'dispatcher_pending':bool(self.store.pending(self.role))})
                        self.engine.checkpoint(decision)
                    self.latest=(st,slot,received,decision,end);last_error=None
                except (RuntimeError,ValueError,OSError,KeyError) as error:
                    self.latest=None;reason=str(error) if isinstance(error,RuntimeError) else type(error).__name__
                    if reason!=last_error:self.store.event(self.role,'compiled_paused',{'reason':reason});last_error=reason
                await asyncio.sleep(.001 if self.mandate.get('compact_feed',False) else .005)
        finally:
            self.latest=None
            if self.engine:self.engine.close()

async def actor(role,folder,private,manifest,mandate,store,cache,proposal):
    source=Events(role,private,manifest,mandate,store,cache,proposal)
    pump=asyncio.create_task(source.pump());next_action=0;last_quote=None;last_error=None
    try:
        async with Net() as net:
            if await net.call('getGenesisHash',[])!=GENESIS:raise RuntimeError('wrong chain')
            tx=Tx(role,Keypair.from_json((private/f'session-{role}.json').read_text()),manifest,mandate,net,store)
            await tx.recover(True);store.event(role,'compiled_actor_started',{'pid':os.getpid(),'program_hash':mandate['program_hash']})
            while source.active():
                try:
                    if store.pending(role):await tx.recover();last_quote=None;continue
                    if store.count(role)>=mandate['actor_transaction_limit']-1:break
                    if source.engine and hasattr(source.engine,'advance'):
                        if await source.engine.advance(tx):source.latest=None;last_quote=None;continue
                    try:source.owner_gate();latest=source.latest
                    except RuntimeError:latest=None
                    if latest is None:
                        if is_maker(role,manifest):
                            st,_,received=await tx.current()
                            if any(o['seat']==role for o in st['orders']):await tx.send(st,7,struct.pack('<Q',65535),source_ns=received,kind='compiled-safety-cancel');last_quote=None
                        await asyncio.sleep(.05);continue
                    st,slot,received,d,decided=latest
                    if time.monotonic_ns()-received>3_000_000_000:raise RuntimeError('candidate source stale')
                    if time.time()<next_action:await asyncio.sleep(.01);continue
                    if isinstance(st,bytes):st=snapshot(st);st['read_slot']=slot;market_ok(st,slot,manifest)
                    if slot+60>=st['seats'][role]['session_expiry']:
                        store.event(role,'session_expiry_stop',{'slot':slot,'expiry':st['seats'][role]['session_expiry']});break
                    if st['seats'][role]['session_turnover_remaining']<st['oracle']:break
                    # Resting order lease is distinct from proposal compilation
                    # freshness and the finite owner-authorized program lease.
                    until=min(int(time.time())+10,int(mandate['ends_unix']))
                    if until<=time.time()+2:break
                    if is_maker(role,manifest):
                        if d.get('policy_id') is not None and d['policy_id']!=source.engine.policy_id:source.latest=None;continue
                        if time.monotonic_ns()>=d['valid_until_ns']:raise RuntimeError('candidate expired before dispatch')
                        fingerprint=[(q['slot'],q['price'],q['lots'],q['reduce']) for q in d['quotes']]
                        own=[o for o in st['orders'] if o['seat']==role]
                        renew=not own or min(o['policy_until'] for o in own)<time.time()+4
                        changed=last_quote!=fingerprint or sum(o['lots'] for o in own)!=sum(q['lots'] for q in d['quotes'])
                        if not changed and not renew:await asyncio.sleep(.01);continue
                        expiry=min(slot+30,st['seats'][role]['session_expiry'])
                        cycle=store.count(role)
                        frame=quotes([(q['slot'],q['price'],q['lots'],expiry,cycle*16+q['slot'],q['reduce']) for q in d['quotes']],until,slide=mandate.get('slide_quotes',False))
                        kind='adaptive-quote:'+d['persona_id']+':'+d['program_hash'] if d.get('persona_id') else 'adaptive-drain' if d.get('adaptive_phase') else 'compiled-quote'
                        result=await tx.send(st,5,frame,policy_until=until,source_ns=received,decision_ns=decided,kind=kind)
                        last_quote=fingerprint if result and result['state']=='CONFIRMED' else None;next_action=time.time()+mandate.get('quote_interval_seconds',.1)
                    else:
                        q=st['seats'][role]['position'];count=store.count_op(role,6)
                        side=1 if q>=6 else 0 if q<=-6 else (0 if ((count//3)+role)%2==0 else 1)
                        live=[o for o in st['orders'] if o['seat']!=role and o['side']!=side and o['expiry']>=slot and o['policy_until']>time.time()+2]
                        if not live:await asyncio.sleep(.05);continue
                        price=min(o['price'] for o in live) if side==0 else max(o['price'] for o in live)
                        if abs(q+(2 if side==0 else -2))>16:raise RuntimeError('flow cap')
                        await tx.send(st,6,ioc(side,2,price,budget=price*2,policy_until=until),policy_until=until,source_ns=received,kind='compiled-synthetic-demand');next_action=time.time()+mandate.get('flow_interval_seconds',3)
                    last_error=None
                except (RuntimeError,ValueError,OSError,KeyError) as error:
                    reason=str(error) if isinstance(error,RuntimeError) else type(error).__name__
                    if reason!=last_error:store.event(role,'dispatch_paused',{'reason':reason});last_error=reason
                    await asyncio.sleep(.05)
            try:
                await tx.recover(True)
                if time.time()<mandate['ends_unix']:
                    st,_,received=await tx.current()
                    if any(o['seat']==role for o in st['orders']):await tx.send(st,7,struct.pack('<Q',65535),source_ns=received,kind='compiled-stop-cancel')
            except RuntimeError as error:store.event(role,'shutdown_pending',{'reason':str(error)})
    finally:
        pump.cancel()
        try:await pump
        except asyncio.CancelledError:pass

async def main(run,role):
    private=BASE/'private'/run;folder=BASE/'evidence'/run
    mandate=json.loads((private/'operating-mandate.json').read_text());manifest=json.loads((folder/'manifest.json').read_text());proposal=json.loads((folder/'compiled-proposal.json').read_text())
    validate(manifest)
    if role not in range(-1,keeper(manifest)+1):raise RuntimeError('unknown actor')
    if mandate['version']!=3 or mandate['program_hash']!=proposal['program_hash']:raise RuntimeError('program mandate mismatch')
    lock=(private/f'role-{role}.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store=Store(private/'runtime.sqlite',mandate)
    if role==-1:return await publish_feed(folder,private,manifest,mandate,store)
    while not (private/'market.cache').exists():
        if time.time()>=mandate['ends_unix'] or (private/'stop').exists():return
        await asyncio.sleep(.05)
    cache=Cache(private/'market.cache')
    try:
        if role==keeper(manifest):await old_actor(role,folder,private,manifest,mandate,store,cache)
        else:await actor(role,folder,private,manifest,mandate,store,cache,proposal)
    finally:cache.close()
if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--run',required=True);a.add_argument('--role',type=int,choices=range(-1,257),required=True);v=a.parse_args()
    if not v.run.startswith('devnet-agents-') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in v.run):raise SystemExit('run name')
    asyncio.run(main(v.run,v.role))
