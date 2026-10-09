"""Scoped strategy handoff on existing signers/journals. No wallet creation."""
import ctypes as C,hashlib,json,os,stat,struct,sys,time
from collections import deque
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'evolution'))
from schema import digest,validate_program,validate_profile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.gateway import InstalledEngine as Engine
from feed_batch import Input,INPUT_BYTES

class MarketHistory:
    def __init__(self):self.samples=deque(maxlen=12)
    def update(self,mark,now):
        if not self.samples or now-self.samples[-1][0]>=2_000_000_000:self.samples.append((now,mark))
        previous=next((p for t,p in reversed(self.samples) if t<=now-2_000_000_000),self.samples[0][1])
        window=[p for t,p in self.samples if t>=now-16_000_000_000]+[mark]
        return max(-100,min(100,(mark-previous)*10000//previous)),min(100,max(window)-min(window))

def validate_grant(g,mandate,role,now,allow_expired=False):
    if g.get('version')!=1 or g.get('mode')!='devnet-persona-canary' or g.get('base_mandate_hash')!=digest(mandate):raise ValueError('adaptive base scope')
    if g.get('program')!=mandate['program'] or g.get('market')!=mandate['market'] or mandate['cluster']!='devnet':raise ValueError('adaptive market')
    if g.get('roles')!=list(range(6)) or role not in g['roles'] or g['ends_unix']>mandate['ends_unix']-60 or now>=mandate['ends_unix'] or (now>=g['ends_unix'] and not allow_expired):raise ValueError('adaptive time/role scope')
    if not 60<=g.get('episode_seconds',0)<=180 or len(g.get('personas',[]))!=30:raise ValueError('adaptive episode scope')
    ids=set()
    for x in g['personas']:
        validate_program(x['program']);validate_profile(x['profile'])
        if x['id'] in ids or x['program_hash']!=digest(x['program']) or x['source']!='muse-live-candidate':raise ValueError('adaptive candidate identity')
        ids.add(x['id'])
        if x['profile']['limits']['position']>8 or x['profile']['limits']['clip']>2:raise ValueError('adaptive risk widened')
    if g.get('grant_hash')!=digest({k:v for k,v in g.items() if k!='grant_hash'}):raise ValueError('adaptive grant hash')
    return g

class Adaptive:
    def __init__(self,base,role,private,mandate,store):
        self.base=base;self.role=role;self.private=private;self.mandate=mandate;self.store=store;self.path=private/'adaptive-grant.json'
        if stat.S_IMODE(self.path.stat().st_mode)&0o077:raise ValueError('adaptive grant permissions')
        self.raw=self.path.read_bytes();self.grant=validate_grant(json.loads(self.raw),mandate,role,time.time(),allow_expired=True);self.plan=self.grant['personas'][role::6]
        self.hash=self.grant['grant_hash'];self.engine=None;self.last_equity=None;self.last_position=None;self.next_check=0;self.history=MarketHistory()
        store.db.executescript('CREATE TABLE IF NOT EXISTS adaptive_grant(hash TEXT PRIMARY KEY,body TEXT); CREATE TABLE IF NOT EXISTS adaptive_state(role INTEGER PRIMARY KEY,grant_hash TEXT,body TEXT); CREATE TABLE IF NOT EXISTS adaptive_episode(role INTEGER,number INTEGER,grant_hash TEXT,body TEXT,PRIMARY KEY(role,number));')
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO adaptive_grant VALUES(?,?)',(self.hash,json.dumps(self.grant,sort_keys=True)))
            row=store.db.execute('SELECT grant_hash,body FROM adaptive_state WHERE role=?',(role,)).fetchone()
            if row:
                if row['grant_hash']!=self.hash:raise ValueError('adaptive grant changed')
                self.state=json.loads(row['body'])
            else:self.state={'phase':'DRAINING','index':0,'episode':None,'reason':'initial_handoff'};self.persist()
        # Expiry forbids another experimental quote, but must not prevent a
        # restarted original worker from draining or resuming its baseline.
        if time.time()>=self.grant['ends_unix'] and self.state['phase']=='ACTIVE':
            self.state.update(phase='DRAINING',reason='deadline');self.persist()
        if self.state['phase']=='ACTIVE':self.install()
        self.policy_id=self.policy_identity()
    def policy_identity(self):return self.hash[:16]+':'+str(self.state['index'])+':'+self.state['phase']
    def persist(self):
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO adaptive_state VALUES(?,?,?)',(self.role,self.hash,json.dumps(self.state,sort_keys=True)))
        self.policy_id=self.policy_identity()
    def install(self):
        if self.engine:self.engine.close()
        x=self.plan[self.state['index']];now=time.monotonic_ns();expiry=now+int(max(0,self.mandate['ends_unix']-time.time())*1e9)
        self.engine=Engine(x['program'],x['profile'],now=now,expires=expiry)
    def checkpoint(self,d):pass  # base and DAG checkpoints occur in decide_prepared
    def close(self):
        if self.engine:self.engine.close()
        self.base.close()
    def decide_prepared(self,data,prepared,prepared_wall,slot,observed,previous_mark):
        decision,old_features,mark=self.base.decide_prepared(data,prepared,prepared_wall,slot,observed,previous_mark);self.base.checkpoint(decision)
        if self.state['phase']=='DONE':return decision,old_features,mark
        wall=int(time.time());now=time.monotonic_ns();x=Input()
        if wall==prepared_wall:x=Input.from_buffer_copy(prepared[self.role*INPUT_BYTES:(self.role+1)*INPUT_BYTES])
        else:
            code=self.base.lib.mp_decode(data,len(data),self.role,slot,observed,now,wall,previous_mark,C.byref(x))
            if code:raise RuntimeError('adaptive market decode')
        a=512+self.role*256;collateral=struct.unpack_from('<Q',data,a+112)[0];quote=int.from_bytes(data[a+128:a+144],'little',signed=True)
        funding=struct.unpack_from('<q',data,176)[0];funding_snapshot=struct.unpack_from('<q',data,a+144)[0];tick_value=struct.unpack_from('<Q',data,168)[0]
        self.last_equity=collateral+quote-x.position*(funding-funding_snapshot)+x.position*x.mark*tick_value;self.last_position=x.position
        momentum,volatility=self.history.update(mark,now)
        age=wall-struct.unpack_from('<q',data,224)[0]
        if not 0<=age<=90 or now-observed>=3_000_000_000:raise RuntimeError('adaptive stale oracle/source')
        f=[x.position,max(-100,min(100,x.imbalance//100)),momentum,volatility,min(4096,x.depth),age]
        if self.state['phase'] in ('DRAINING','FENCED'):
            qs=[]
            if x.position:
                side=1 if x.position>0 else 0
                price=max(mark-24,x.bid+1) if side else min(mark+24,x.ask-1 if x.ask else mark+24)
                qs=[{'slot':side*8,'price':price,'lots':min(2,abs(x.position)),'reduce':True}]
            decision.update(quotes=qs,reduce=True,mode=9,policy_id=self.policy_id,program_hash='drain',adaptive_phase=self.state['phase'])
        else:
            d=self.engine.decide(f,mark,x.bid,x.ask,now=now);target=self.plan[self.state['index']]
            decision.update(quotes=[{'slot':q['side']*8,'price':q['price'],'lots':q['lots'],'reduce':q['reduce']} for q in d['quotes']],
                reduce=d['reduce'],kernel_ns=d['kernel_ns'],policy_id=self.policy_id,program_hash=target['program_hash'],persona_id=target['id'],adaptive_phase='ACTIVE',mode=10)
        return decision,dict(old_features,adaptive=f),mark
    async def advance(self,tx):
        if time.monotonic()<self.next_check:return False
        self.next_check=time.monotonic()+.5
        if self.path.read_bytes()!=self.raw:raise RuntimeError('adaptive grant tampered')
        phase=self.state['phase'];now=time.time()
        if phase=='DONE':return False
        if phase=='ACTIVE':
            episode=self.state['episode'];limit=self.plan[self.state['index']]['profile']['limits']['loss_ticks']
            reason='duration' if now>=episode['started_unix']+self.grant['episode_seconds'] else 'deadline' if now>=self.grant['ends_unix'] else 'loss_stop' if self.last_equity is not None and self.last_equity<=episode['start_equity']-limit else None
            if reason is None:return False
            self.state.update(phase='DRAINING',reason=reason);self.persist();self.store.event(self.role,'adaptive_drain_requested',{'index':self.state['index'],'reason':reason,'policy_id':self.policy_id});return True
        if self.last_position not in (None,0):return False
        # Cancellation and finalized reconciliation are mandatory even if the
        # cached position was flat; a resting quote may fill in the meantime.
        await tx.recover(True)
        if self.store.pending(self.role):return False
        st,_,received=await tx.current()
        if st['seats'][self.role]['position']:return False
        if any(o['seat']==self.role for o in st['orders']):await tx.send(st,7,struct.pack('<Q',65535),source_ns=received,kind='adaptive-handoff-cancel');await tx.recover(True)
        if self.store.pending(self.role):return False
        st,slot,_=await tx.current(True);seat=st['seats'][self.role]
        if seat['position'] or any(o['seat']==self.role for o in st['orders']):return False
        last=self.store.last_confirmed(self.role)
        if last and (last.get('confirmation_status')!='finalized' or seat['sequence']<last['sequence']):return False
        episode=self.state['episode']
        if episode:
            episode.update(ended_unix=now,end_equity=seat['equity'],pnl_tokens=seat['equity']-episode['start_equity'],end_slot=slot,end_sequence=seat['sequence'],
                reason=self.state['reason'],flat=True,pending_signatures=0,open_orders=0,tx_count_end=self.store.count(self.role),
                end_control_equity={str(r):st['seats'][r]['equity'] for r in (6,7)})
            episode['control_mean_pnl']=sum(episode['end_control_equity'][str(r)]-episode['start_control_equity'][str(r)] for r in (6,7))/2
            episode['excess_vs_control']=episode['pnl_tokens']-episode['control_mean_pnl']
            with self.store.db:self.store.db.execute('INSERT OR IGNORE INTO adaptive_episode VALUES(?,?,?,?)',(self.role,self.state['index'],self.hash,json.dumps(episode)))
            self.store.event(self.role,'adaptive_episode_completed',episode);self.state['index']+=1;self.state['episode']=None
        if self.state['index']>=len(self.plan) or now>=self.grant['ends_unix']:
            self.state['phase']='DONE';self.persist()
            if self.engine:self.engine.close();self.engine=None
            self.store.event(self.role,'adaptive_restored_baseline',{'grant_hash':self.hash,'slot':slot,'flat':True});return True
        target=self.plan[self.state['index']]
        self.state.update(phase='ACTIVE',episode={'persona_id':target['id'],'name':target['name'],'program_hash':target['program_hash'],'profile_hash':target['profile']['profile_hash'],
            'source_attempt':target['source_attempt'],'admission':'bounded_devnet_canary','research_champion':target['research_champion'],
            'started_unix':time.time(),'start_equity':seat['equity'],'start_slot':slot,'start_sequence':seat['sequence'],'tx_count_start':self.store.count(self.role),
            'initial_position':0,'initial_open_orders':0,'initial_pending':0,'start_control_equity':{str(r):st['seats'][r]['equity'] for r in (6,7)}})
        self.install();self.persist();self.store.event(self.role,'adaptive_installed',dict(self.state['episode'],grant_hash=self.hash,policy_id=self.policy_id));return True
