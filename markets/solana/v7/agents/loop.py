"""Owner-scoped devnet execution adapter for Economic Machine's C++ runtime.

One process owns this market lane. Quote frames are compiled without inference.
Prepared transactions are never automatically re-signed or retried on ambiguity.
A restarted completed run performs reads only. No production control is enabled.
"""
import argparse,base64,fcntl,hashlib,json,os,struct,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
from devnet import Devnet,BASE,GENESIS,rpc,key
from wire import *
from solders.keypair import Keypair
from solders.hash import Hash
from solders.message import Message
from solders.transaction import VersionedTransaction
from solders.compute_budget import set_compute_unit_limit
from native import Native
from journal import Journal
from runtime_config import ACTORS,PROGRAM,ELF_HASH,ELF_PATH

FLOW=[2,2,4,-2,-2,-4,4,-4]
def save(path,obj):
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w') as f:json.dump(obj,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    fd=os.open(path.parent,os.O_DIRECTORY);os.fsync(fd);os.close(fd)

def admit(st,slot,owners):
    if st['halted'] or slot<st['oracle_slot'] or slot-st['oracle_slot']>100:raise RuntimeError('halted or stale oracle')
    if len(st['seats'])!=3:raise RuntimeError('unexpected seat set')
    for i,s in enumerate(st['seats']):
        if s['seat']!=i or s['owner']!=owners[i]:raise RuntimeError('seat owner mismatch')
        if abs(s['position'])>(8 if i<2 else 16):raise RuntimeError('position mandate exceeded')
        if min(s['equity'],s['collateral'])<500_000:raise RuntimeError('collateral reserve exhausted')

def entries(decision,slot,cycle):
    return [(q['slot'],q['price'],q['lots'],slot+60,cycle*16+q['slot'],q['reduce']) for q in decision['quotes']]

class Loop:
    def __init__(self,run,limits=None):
        self.folder=BASE/'evidence'/run;self.private=BASE/'private'/run
        self.lock=(BASE/'private/agent-loop.lock').open('a');fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not self.folder.exists():
            d=Devnet(run);self.d=d
            mandate={'version':1,'cluster':'devnet','program':PROGRAM,'market':str(d.market.pubkey()),'makers':[0,1],'flow_agent':2,
                'source':'owner-authorized bounded development fixture; fixed catalog 2; no current Muse inference',
                'cycles':len(FLOW),'flow_lots':FLOW,'maker_position_limit':8,'flow_position_limit':16,'clip_lots':2,
                'gross_turnover_limit':2_000_000,'max_transactions':48,'created_unix':int(time.time()),'quote_seconds':600,'cleanup_seconds':1800,
                'deposit_per_agent':1_000_000,'max_setup_lamports':1_500_000_000}
            if limits:mandate.update(limits)
            save(self.folder/'mandate.json',mandate)
        else:
            # Restore public identity from the immutable manifest; never create replacement keys.
            d=Devnet.__new__(Devnet);d.folder=self.folder;d.private=self.private
            manifest=json.loads((self.folder/'manifest.json').read_text());d.manifest=manifest
            def load(path):return Keypair.from_json(path.read_text())
            d.payer=load(BASE/'private/devnet-payer.json');d.program=Pubkey.from_string(manifest['program'])
            d.market=load(self.private/'market.json');d.mint=load(self.private/'mint.json');d.vault=load(self.private/'vault.json')
            d.actors=[load(self.private/f'agent-{s}.json') for s in range(ACTORS)];d.tokens=[load(self.private/f'token-{s}.json') for s in range(ACTORS)]
            d.auth,d.bump=Pubkey.find_program_address([b'vault',bytes(d.market.pubkey())],d.program);self.d=d
            if any(str(k.pubkey())!=manifest[n] for k,n in [(d.market,'market'),(d.mint,'mint'),(d.vault,'vault'),(d.payer,'payer')]):raise RuntimeError('manifest identity mismatch')
            if [str(a.pubkey()) for a in d.actors]!=manifest['agents']:raise RuntimeError('agent identity mismatch')
        self.mandate=json.loads((self.folder/'mandate.json').read_text());self.j=Journal(self.private/'execution.sqlite',self.mandate)
        self.owners=[str(a.pubkey()) for a in self.d.actors]
        if rpc('getGenesisHash',[])!=GENESIS or str(self.d.program)!=PROGRAM:raise RuntimeError('cluster/program mismatch')
        elf=ELF_PATH.read_bytes()
        if hashlib.sha256(elf).hexdigest()!=ELF_HASH:raise RuntimeError('ELF pin mismatch')
        p=rpc('getAccountInfo',[PROGRAM,{'encoding':'base64','commitment':'finalized'}])['value']
        if not p or not p['executable']:raise RuntimeError('program not executable')
        state=base64.b64decode(p['data'][0]);program_data=Pubkey.from_bytes(state[4:36])
        deployed=rpc('getAccountInfo',[str(program_data),{'encoding':'base64','commitment':'finalized'}])['value']
        if base64.b64decode(deployed['data'][0])[45:45+len(elf)]!=elf:raise RuntimeError('deployed ELF mismatch')
        self.native=[];self.min_slot=0
    def deadline(self,cleanup=False):
        limit=self.mandate['cleanup_seconds'] if cleanup else self.mandate['quote_seconds']
        if time.time()>=self.mandate['created_unix']+limit:raise RuntimeError('mandate expired; reconciliation only')
    def read(self,finalized=False):
        start=time.monotonic_ns()
        keys=[self.d.market.pubkey(),self.d.vault.pubkey(),* [t.pubkey() for t in self.d.tokens]]
        result=rpc('getMultipleAccounts',[[str(k) for k in keys],{'encoding':'base64','commitment':'finalized' if finalized else 'confirmed','minContextSlot':self.min_slot}])
        values=result['value'];slot=result['context']['slot']
        self.min_slot=max(self.min_slot,slot)
        if any(v is None for v in values):raise RuntimeError('missing account')
        if values[0]['owner']!=PROGRAM or any(v['owner']!=str(TOKEN) for v in values[1:]):raise RuntimeError('account owner mismatch')
        blobs=[base64.b64decode(v['data'][0]) for v in values];data,vault=blobs[:2]
        if len(data)!=SIZE or data[:8]!=MAGIC or data[40:72]!=bytes(self.d.mint.pubkey()) or data[72:104]!=bytes(self.d.vault.pubkey()):raise RuntimeError('market identity mismatch')
        for i,blob in enumerate(blobs[1:]):
            owner=self.d.auth if i==0 else self.d.actors[i-1].pubkey()
            if len(blob)!=165 or blob[:32]!=bytes(self.d.mint.pubkey()) or blob[32:64]!=bytes(owner):raise RuntimeError('token identity mismatch')
        st=assert_conservation(data,struct.unpack_from('<Q',vault,64)[0]);st['read_slot']=slot
        st['wallet_tokens']=[struct.unpack_from('<Q',b,64)[0] for b in blobs[2:]]
        st['vault_tokens']=struct.unpack_from('<Q',vault,64)[0]
        return st,slot,start
    def command(self,st,s,op,payload=b'',extras=()):
        a=st['seats'][s]
        return command(self.d.program,self.d.market.pubkey(),self.d.actors[s].pubkey(),op,s,payload,
            seq=a['sequence']+1,epoch=a['epoch'],deadline=st['read_slot']+100,observed=st['market_sequence'],extras=extras)
    def oracle(self):return command(self.d.program,self.d.market.pubkey(),self.d.payer.pubkey(),1,payload=struct.pack('<QI',15000,10))
    def reconcile(self,label,finalized=False):
        row=self.j.get(label);end=time.monotonic()+50
        if row['state'] in ('EXPIRED_UNLANDED','FAILED_FINALIZED'):return
        while time.monotonic()<end:
            status=rpc('getSignatureStatuses',[[row['signature']],{'searchTransactionHistory':True}])['value'][0]
            if status is None and finalized and row['state']=='PREPARED' and label.startswith('setup-agent-') and label.removeprefix('setup-agent-').isdigit():
                height=rpc('getBlockHeight',[{'commitment':'finalized'}])
                if height>json.loads(row['context'])['last_valid_block_height']:
                    s=int(label.removeprefix('setup-agent-'));minimum=rpc('getSlot',[{'commitment':'finalized'}])
                    r=rpc('getMultipleAccounts',[[str(self.d.market.pubkey()),str(self.d.tokens[s].pubkey())],{'encoding':'base64','commitment':'finalized','minContextSlot':minimum}])
                    market,token=r['value'];data=base64.b64decode(market['data'][0])
                    proof={'signature':row['signature'],'finalized_height':height,'read_slot':r['context']['slot'],
                           'token_absent':token is None,'seat_absent':data[SEAT+s*256+168]==0}
                    self.j.retire_unlanded_setup(label,proof)
                    save(self.folder/(label+'.expired-unlanded.json'),proof);return
            if status and status['err'] is not None:raise RuntimeError(f'{label}: chain rejection; stopped with journal retained')
            if status and status.get('confirmationStatus') in (('finalized',) if finalized else ('confirmed','finalized')):
                receipt=rpc('getTransaction',[row['signature'],{'encoding':'json','commitment':'finalized' if finalized else 'confirmed','maxSupportedTransactionVersion':0}])
                if receipt:
                    self.j.confirm(label,receipt);self.min_slot=max(self.min_slot,receipt['slot'])
                    save(self.folder/(label+'.receipt.json'),receipt)
                    save(self.folder/(label+'.decision.json'),json.loads(row['context']))
                    return receipt
            time.sleep(1)
        raise RuntimeError(f'{label}: UNKNOWN; reservation retained; reconcile before any new transaction')
    def execute(self,label,ixs,signers=(),reservation=0,context=None,cleanup=False):
        if self.j.get(label):return
        self.deadline(cleanup)
        context=dict(context or {},program=PROGRAM)
        bh=rpc('getLatestBlockhash',[{'commitment':'confirmed'}])['value']
        signer_set={str(k.pubkey()):k for k in [self.d.payer,*signers]}
        tx=VersionedTransaction(Message.new_with_blockhash([set_compute_unit_limit(400_000),*ixs],self.d.payer.pubkey(),Hash.from_string(bh['blockhash'])),list(signer_set.values()))
        if len(bytes(tx))>1232:raise RuntimeError('transaction exceeds packet bound')
        raw=base64.b64encode(bytes(tx)).decode();signature=str(tx.signatures[0]);context['last_valid_block_height']=bh['lastValidBlockHeight']
        sim=rpc('simulateTransaction',[raw,{'encoding':'base64','sigVerify':True,'commitment':'confirmed'}])['value']
        if sim['err'] is not None:
            save(self.folder/(label+'.simulation-failure.json'),sim);raise RuntimeError(f'{label}: preflight rejected')
        if 'decisions' in context and time.monotonic_ns()>=min(d['valid_until_ns'] for d in context['decisions']):raise RuntimeError('quote decision expired before broadcast')
        self.deadline(cleanup)
        self.j.prepare(label,signature,raw,reservation,context)
        # An exception from here leaves PREPARED. No blanket transport retry.
        returned=rpc('sendTransaction',[raw,{'encoding':'base64','skipPreflight':False,'preflightCommitment':'confirmed','maxRetries':3}])
        if returned!=signature:raise RuntimeError('signature mismatch; retained UNKNOWN')
        receipt=self.reconcile(label)
        save(self.folder/(label+'.decision.json'),context)
        print(json.dumps({'label':label,'signature':signature,'cu':receipt['meta'].get('computeUnitsConsumed')}),flush=True)
    def setup(self):
        d=self.d
        balance=rpc('getBalance',[str(d.payer.pubkey()),{'commitment':'finalized'}])['value']
        rent=sum(rpc('getMinimumBalanceForRentExemption',[size]) for size in [SIZE,82,165,165,165,165])
        if rent>self.mandate['max_setup_lamports'] or (not self.j.get('setup-market') and balance<rent+10_000_000):raise RuntimeError('setup devnet SOL bound')
        self.execute('setup-market',[d.create(d.market,SIZE,d.program),d.create(d.mint,82,TOKEN),Instruction(TOKEN,bytes([20,6])+bytes(d.payer.pubkey())+b'\0',[meta(d.mint.pubkey(),True)])],[d.market,d.mint])
        self.execute('setup-vault',[d.create(d.vault,165,TOKEN),d.init_token(d.vault,d.auth),command(d.program,d.market.pubkey(),d.payer.pubkey(),0,payload=bytes([d.bump])+struct.pack('<Q',1),extras=[meta(d.mint.pubkey()),meta(d.vault.pubkey(),True),meta(d.auth),meta(TOKEN)],market_signer=True)],[d.vault,d.market])
        self.after_vault()
        for s,(actor,tok) in enumerate(zip(d.actors,d.tokens)):
            extra=[meta(d.vault.pubkey(),True),meta(tok.pubkey(),True),meta(d.auth),meta(TOKEN)]
            self.execute(f'setup-agent-{s}',[self.oracle(),d.create(tok,165,TOKEN),d.init_token(tok,actor.pubkey()),Instruction(TOKEN,bytes([7])+struct.pack('<Q',1_000_000),[meta(d.mint.pubkey(),True),meta(tok.pubkey(),True),meta(d.payer.pubkey(),False,True)]),command(d.program,d.market.pubkey(),actor.pubkey(),2,s,struct.pack('<Q',8 if s<2 else 16)),command(d.program,d.market.pubkey(),actor.pubkey(),3,s,struct.pack('<Q',1_000_000),extras=extra)],[actor,tok])
    def after_vault(self):pass
    def run(self,reconcile_only=False):
        # On restart resolve every already submitted signature at finalized commitment.
        for row in self.j.rows():self.reconcile(row['label'],True)
        if reconcile_only or (self.folder/'final.json').exists():
            st,_,_=self.read(True);print(json.dumps({'read_only':True,'state':st,'journal':self.j.summary()}));return
        self.setup()
        expiry=time.monotonic_ns()+max(1,int(self.mandate['created_unix']+600-time.time()))*1_000_000_000
        if any(not self.j.get(f'cycle-{i:02d}') for i in range(len(FLOW))):
            self.deadline();self.native=[Native(s,expiry) for s in (0,1)]
        for cycle,flow in enumerate(FLOW):
            label=f'cycle-{cycle:02d}'
            if self.j.get(label):continue
            self.deadline();st,slot,observed=self.read()
            if slot-st['oracle_slot']>70:
                self.execute(f'cycle-{cycle:02d}-oracle',[self.oracle()]);st,slot,observed=self.read()
            admit(st,slot,self.owners)
            decisions=[engine.decide(st,slot,observed) for engine in self.native]
            if abs(st['seats'][2]['position']+flow)>16:raise RuntimeError('flow position bound')
            side=0 if flow>0 else 1;lots=abs(flow)
            quotes_all=[q for decision in decisions for q in decision['quotes'] if (q['slot']//8)!=side]
            if sum(q['lots'] for q in quotes_all)<lots:raise RuntimeError('insufficient bounded fixture liquidity')
            limit=max(q['price'] for q in quotes_all) if side==0 else min(q['price'] for q in quotes_all)
            ixs=[self.oracle(),*[self.command(st,s,5,quotes(entries(decisions[s],slot,cycle))) for s in (0,1)],self.command(st,2,6,ioc(side,lots,limit,minimum=lots,budget=lots*16500,client=cycle))]
            self.execute(label,ixs,self.d.actors,2*lots*16500,{'lots':lots,'flow':flow,'before':st,'decisions':decisions})
            after,_,_=self.read();admit(after,after['read_slot'],self.owners)
            if after['seats'][2]['position']!=st['seats'][2]['position']+flow:raise RuntimeError('inventory did not match fill')
            save(self.folder/(label+'.state.json'),after)
        self.cleanup()
    def cleanup(self):
        for s in range(3):
            label=f'cancel-{s}'
            if self.j.get(label):continue
            st,_,_=self.read();self.execute(label,[self.command(st,s,7,struct.pack('<Q',65535))],[self.d.actors[s]],cleanup=True)
        # Final reduce-only matching between opposite inventory holders. No external capital.
        for k in range(3):
            label=f'flatten-{k}'
            if self.j.get(label):continue
            st,slot,_=self.read();longs=[s for s in st['seats'] if s['position']>0];shorts=[s for s in st['seats'] if s['position']<0]
            if not longs and not shorts:break
            if not longs or not shorts:raise RuntimeError('unbalanced inventory')
            maker=shorts[0]['seat'];taker=longs[0]['seat'];lots=min(-shorts[0]['position'],longs[0]['position'])
            self.execute(label,[self.oracle(),self.command(st,maker,5,quotes([(0,15000,lots,slot+60,100+k,True)])),self.command(st,taker,6,ioc(1,lots,15000,minimum=lots,budget=lots*15000,reduce=True))], [self.d.actors[maker],self.d.actors[taker]],2*lots*15000,{'lots':lots,'before':st},cleanup=True)
        self.execute('settle',[self.oracle(),command(self.d.program,self.d.market.pubkey(),self.d.payer.pubkey(),10)],cleanup=True)
        for s in range(3):
            label=f'withdraw-{s}'
            if self.j.get(label):continue
            st,_,_=self.read()
            if any(a['position'] or a['quote'] for a in st['seats']) or st['orders']:raise RuntimeError('cleanup not flat')
            extra=[meta(self.d.vault.pubkey(),True),meta(self.d.tokens[s].pubkey(),True),meta(self.d.auth),meta(TOKEN)]
            self.execute(label,[self.oracle(),self.command(st,s,4,struct.pack('<Q',st['seats'][s]['collateral']),extra)],[self.d.actors[s]],cleanup=True)
        for row in self.j.rows():self.reconcile(row['label'],True)
        st,_,_=self.read(True)
        if st['orders'] or any(a['position'] or a['quote'] or a['collateral'] for a in st['seats']):raise RuntimeError('final state not flat')
        if sum(st['wallet_tokens'])+st['vault_tokens']!=3_000_000:raise RuntimeError('token supply not conserved')
        final={'all_landed_transactions_finalized':True,'manifest':self.d.manifest,'mandate':self.mandate,'journal':self.j.summary(),'state':st}
        save(self.folder/'final.json',final)
        for engine in self.native:engine.close()
        print(json.dumps({'finalized':True,'market':str(self.d.market.pubkey()),'journal':self.j.summary(),'fees':st['fees']}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',required=True);parser.add_argument('--reconcile-only',action='store_true');args=parser.parse_args()
    if not args.run.startswith('devnet-agents-') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in args.run):raise SystemExit('invalid run name')
    Loop(args.run).run(args.reconcile_only)
