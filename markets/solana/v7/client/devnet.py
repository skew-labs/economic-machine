"""Bounded devnet-only acceptance run. Never switch RPC/cluster on failure."""
import argparse,base64,hashlib,json,struct,time,urllib.request,urllib.error,urllib.parse
from pathlib import Path
from wire import *
from solders.keypair import Keypair
from solders.hash import Hash
from solders.message import Message
from solders.transaction import VersionedTransaction
from solders.system_program import create_account
from solders.compute_budget import set_compute_unit_limit

from runtime_config import BASE,ACTORS,ELF_PATH,PROGRAM_KEY
RPC=(BASE/'private/devnet-rpc.url').read_text().strip() if (BASE/'private/devnet-rpc.url').exists() else 'https://api.devnet.solana.com'
assert urllib.parse.urlparse(RPC).scheme=='https' and urllib.parse.urlparse(RPC).hostname in ('api.devnet.solana.com','devnet.helius-rpc.com'),'unapproved devnet RPC host'
GENESIS='EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG'

def rpc(method,params):
    req=urllib.request.Request(RPC,data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=30) as r:d=json.load(r)
    except (urllib.error.URLError,TimeoutError) as e:raise RuntimeError('RPC transport failed; inspect journal before retry') from None
    if 'error' in d:raise RuntimeError(f'RPC {method} error code: {d["error"].get("code")}')
    return d['result']

def account(k,commitment='confirmed'):
    r=rpc('getAccountInfo',[str(k),{'encoding':'base64','commitment':commitment}])
    if r['value'] is None:return None,r['context']['slot']
    return base64.b64decode(r['value']['data'][0]),r['context']['slot']

def key(path):
    if path.exists():return Keypair.from_json(path.read_text())
    k=Keypair();path.write_text(k.to_json());path.chmod(0o600);return k

class Devnet:
    def __init__(self,run):
        assert rpc('getGenesisHash',[])==GENESIS,'wrong cluster'
        self.folder=BASE/'evidence'/run; self.folder.mkdir(exist_ok=False)
        self.private=BASE/'private'/run;self.private.mkdir(mode=0o700)
        self.payer=Keypair.from_json((BASE/'private/devnet-payer.json').read_text())
        self.program=Keypair.from_json(PROGRAM_KEY.read_text()).pubkey()
        self.market=key(self.private/'market.json');self.mint=key(self.private/'mint.json');self.vault=key(self.private/'vault.json')
        self.actors=[key(self.private/f'agent-{i}.json') for i in range(ACTORS)];self.tokens=[key(self.private/f'token-{i}.json') for i in range(ACTORS)]
        self.auth,self.bump=Pubkey.find_program_address([b'vault',bytes(self.market.pubkey())],self.program)
        self.seq=[0]*ACTORS;self.records=[]
        self.manifest={'cluster':'devnet','genesis':GENESIS,'program':str(self.program),'market':str(self.market.pubkey()),'mint':str(self.mint.pubkey()),'vault':str(self.vault.pubkey()),'payer':str(self.payer.pubkey()),'agents':[str(a.pubkey()) for a in self.actors],'elf_sha256':hashlib.sha256(ELF_PATH.read_bytes()).hexdigest(),'oracle':'creator-signed devnet fixture; NOT external price attestation','collateral':'new 6-decimal devnet test token; NOT real USDC'}
        (self.folder/'manifest.json').write_text(json.dumps(self.manifest,indent=2)+'\n')
    def send(self,label,ixs,signers=()):
        bh=rpc('getLatestBlockhash',[{'commitment':'confirmed'}])['value']
        keys={str(k.pubkey()):k for k in [self.payer,*signers]}
        tx=VersionedTransaction(Message.new_with_blockhash([set_compute_unit_limit(400_000),*ixs],self.payer.pubkey(),Hash.from_string(bh['blockhash'])),list(keys.values()))
        raw=base64.b64encode(bytes(tx)).decode();sig=str(tx.signatures[0])
        record={'label':label,'signature':sig,'last_valid_block_height':bh['lastValidBlockHeight'],'state':'prepared'}
        # A prepared journal is mandatory before any broadcast. Rerun uses a NEW
        # run directory; failed/ambiguous runs are reconciled, never replayed blindly.
        (self.folder/(label+'.signed.json')).write_text(json.dumps({'record':record,'transaction':raw},indent=2)+'\n')
        sim=rpc('simulateTransaction',[raw,{'encoding':'base64','sigVerify':True,'commitment':'confirmed'}])
        record['simulation']=sim
        if sim['value']['err'] is not None:
            (self.folder/(label+'.json')).write_text(json.dumps(record,indent=2));raise RuntimeError(f'{label} preflight failed: {sim["value"]}')
        record['state']='broadcast-attempted';(self.folder/(label+'.json')).write_text(json.dumps(record,indent=2))
        returned=rpc('sendTransaction',[raw,{'encoding':'base64','skipPreflight':False,'maxRetries':3,'preflightCommitment':'confirmed'}]);assert returned==sig
        deadline=time.monotonic()+75
        while time.monotonic()<deadline:
            status=rpc('getSignatureStatuses',[[sig],{'searchTransactionHistory':True}])['value'][0]
            if status and status['err'] is not None:raise RuntimeError(f'{label} chain failure {status}')
            if status and status.get('confirmationStatus') in ('confirmed','finalized'):break
            time.sleep(1.2)
        else:raise RuntimeError(f'{label} UNKNOWN; reconcile {sig}; do not resubmit')
        receipt=None
        for _ in range(5):
            receipt=rpc('getTransaction',[sig,{'encoding':'json','commitment':'confirmed','maxSupportedTransactionVersion':0}])
            if receipt is not None:break
            time.sleep(1)
        assert receipt is not None and receipt['meta']['err'] is None
        record.update(state='confirmed',receipt=receipt)
        (self.folder/(label+'.json')).write_text(json.dumps(record,indent=2)+'\n');self.records.append(record)
        print(json.dumps({'label':label,'signature':sig,'slot':receipt['slot'],'cu':receipt['meta'].get('computeUnitsConsumed')}),flush=True)
    def create(self,k,size,owner):
        return create_account({'from_pubkey':self.payer.pubkey(),'to_pubkey':k.pubkey(),'lamports':rpc('getMinimumBalanceForRentExemption',[size]),'space':size,'owner':owner})
    def init_token(self,k,owner):return Instruction(TOKEN,bytes([18])+bytes(owner),[meta(k.pubkey(),True),meta(self.mint.pubkey())])
    def slot(self):return rpc('getSlot',[{'commitment':'confirmed'}])
    def ix(self,s,op,payload=b'',extras=()):
        self.seq[s]+=1
        return command(self.program,self.market.pubkey(),self.actors[s].pubkey(),op,s,payload,seq=self.seq[s],deadline=self.slot()+150,extras=extras)
    def oracle(self,price,label):
        self.send(label,[command(self.program,self.market.pubkey(),self.payer.pubkey(),1,payload=struct.pack('<QI',price,10))])
    def run(self):
        self.send('01-create-market-mint',[self.create(self.market,SIZE,self.program),self.create(self.mint,82,TOKEN),Instruction(TOKEN,bytes([20,6])+bytes(self.payer.pubkey())+b'\0',[meta(self.mint.pubkey(),True)])],[self.market,self.mint])
        self.send('02-create-vault',[self.create(self.vault,165,TOKEN),self.init_token(self.vault,self.auth),command(self.program,self.market.pubkey(),self.payer.pubkey(),0,payload=bytes([self.bump])+struct.pack('<Q',1),extras=[meta(self.mint.pubkey()),meta(self.vault.pubkey(),True),meta(self.auth),meta(TOKEN)],market_signer=True)],[self.vault,self.market])
        self.oracle(15000,'03-oracle')
        for s,(actor,tok) in enumerate(zip(self.actors,self.tokens)):
            extra=[meta(self.vault.pubkey(),True),meta(tok.pubkey(),True),meta(self.auth),meta(TOKEN)]
            self.send(f'04-agent-{s}',[self.create(tok,165,TOKEN),self.init_token(tok,actor.pubkey()),Instruction(TOKEN,bytes([7])+struct.pack('<Q',1_000_000_000),[meta(self.mint.pubkey(),True),meta(tok.pubkey(),True),meta(self.payer.pubkey(),False,True)]),command(self.program,self.market.pubkey(),actor.pubkey(),2,s,struct.pack('<Q',1_000_000)),self.ix(s,3,struct.pack('<Q',1_000_000_000),extra)],[actor,tok])
        self.oracle(15000,'05-oracle-refresh')
        expiry=self.slot()+100
        self.send('06-replace-4x4',[self.ix(0,5,quotes([(i,14990-i,1000,expiry,i,False) for i in range(4)]+[(8+i,15010+i,1000,expiry,8+i,False) for i in range(4)]))],[self.actors[0]])
        self.send('07-buy-four-makers',[self.ix(1,6,ioc(0,4000,15013,minimum=4000,visits=4))],[self.actors[1]])
        self.send('08-cancel',[self.ix(0,7,struct.pack('<Q',65535))],[self.actors[0]])
        self.oracle(15100,'09-oracle-reprice')
        self.send('10-cash-settle',[command(self.program,self.market.pubkey(),self.payer.pubkey(),10)])
        self.send('11-maker-close-bid',[self.ix(0,5,quotes([(0,15100,4000,self.slot()+100,100,True)]))],[self.actors[0]])
        self.send('12-close-both-positions',[self.ix(1,6,ioc(1,4000,15100,minimum=4000,reduce=True))],[self.actors[1]])
        self.send('13-final-settle',[command(self.program,self.market.pubkey(),self.payer.pubkey(),10)])
        data,_=account(self.market.pubkey());st=snapshot(data)
        assert all(s['position']==0 and s['quote']==0 for s in st['seats'])
        for s in range(3):
            extra=[meta(self.vault.pubkey(),True),meta(self.tokens[s].pubkey(),True),meta(self.auth),meta(TOKEN)]
            self.send(f'14-withdraw-{s}',[self.ix(s,4,struct.pack('<Q',st['seats'][s]['collateral']),extra)],[self.actors[s]])
        sigs=[r['signature'] for r in self.records];until=time.monotonic()+90
        while time.monotonic()<until:
            statuses=rpc('getSignatureStatuses',[sigs,{'searchTransactionHistory':True}])['value']
            if all(x and x['err'] is None and x['confirmationStatus']=='finalized' for x in statuses):break
            time.sleep(2)
        else:raise RuntimeError('finalization pending; do not call run again')
        d,slot=account(self.market.pubkey(),'finalized');v,vslot=account(self.vault.pubkey(),'finalized')
        final=assert_conservation(d,struct.unpack_from('<Q',v,64)[0])
        assert all(s['position']==0 and s['collateral']==0 and s['quote']==0 for s in final['seats'])
        final.update(market_read_slot=slot,vault_read_slot=vslot,all_transactions_finalized=True,manifest=self.manifest)
        (self.folder/'final-reconciliation.json').write_text(json.dumps(final,indent=2)+'\n')
        (BASE/'evidence/devnet-latest.json').write_text(json.dumps({'run':str(self.folder),'manifest':self.manifest,'final':final},indent=2)+'\n')
        print('FINALIZED: positions=0; collateral=0; vault=fees; quote/funding sums=0',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);args=p.parse_args()
    assert args.run.startswith('devnet-') and '/' not in args.run
    Devnet(args.run).run()
