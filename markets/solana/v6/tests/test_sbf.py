"""Execute the real ELF in LiteSVM; all reported CU come from transaction metadata."""
import json,os,struct,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
from wire import *
from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.message import Message
from solders.transaction import VersionedTransaction
from solders.transaction_metadata import TransactionMetadata,FailedTransactionMetadata
from solders.system_program import create_account
from solders.compute_budget import set_compute_unit_limit

ELF=Path(os.environ['MP_ELF']) if 'MP_ELF' in os.environ else Path(__file__).resolve().parents[1]/'program/target/deploy/machine_perps.so'
MEASUREMENTS=[]
class Lab:
    def __init__(self,actors=3,actor_keys=None,tick_value=1,collateral=1_000_000_000):
        self.vm=LiteSVM();self.program=Pubkey.new_unique(); self.vm.add_program_from_file(self.program,ELF)
        self.payer=Keypair();self.vm.airdrop(self.payer.pubkey(),100_000_000_000)
        self.market=Keypair();self.mint=Keypair();self.vault=Keypair()
        self.auth,self.bump=Pubkey.find_program_address([b'vault',bytes(self.market.pubkey())],self.program)
        self.actors=actor_keys or [Keypair() for _ in range(actors)];self.tokens=[Keypair() for _ in self.actors]
        self.seq=[0]*actors;self.epoch=[1]*actors
        self.send([self.create(self.market,SIZE,self.program),self.create(self.mint,82,TOKEN),
                   Instruction(TOKEN,bytes([20,6])+bytes(self.payer.pubkey())+b'\0',[meta(self.mint.pubkey(),True)]),
                   self.create(self.vault,165,TOKEN),self.init_token(self.vault,self.auth)], [self.market,self.mint,self.vault], 'create')
        self.send([command(self.program,self.market.pubkey(),self.payer.pubkey(),0,payload=bytes([self.bump])+struct.pack('<Q',tick_value),
                           extras=[meta(self.mint.pubkey()),meta(self.vault.pubkey(),True),meta(self.auth),meta(TOKEN)],market_signer=True)],[self.market],'initialize')
        self.oracle(15000)
        for s,(actor,tok) in enumerate(zip(self.actors,self.tokens)):
            self.send([self.create(tok,165,TOKEN),self.init_token(tok,actor.pubkey()),
                       Instruction(TOKEN,bytes([7])+struct.pack('<Q',collateral),[meta(self.mint.pubkey(),True),meta(tok.pubkey(),True),meta(self.payer.pubkey(),False,True)]),
                       command(self.program,self.market.pubkey(),actor.pubkey(),2,s,struct.pack('<Q',1_000_000))],[actor,tok],'open-seat')
            self.call(s,3,struct.pack('<Q',collateral),name='deposit')
        self.check()
    def create(self,k,size,owner):
        return create_account({'from_pubkey':self.payer.pubkey(),'to_pubkey':k.pubkey(),'lamports':self.vm.minimum_balance_for_rent_exemption(size),'space':size,'owner':owner})
    def init_token(self,tok,owner):
        return Instruction(TOKEN,bytes([18])+bytes(owner),[meta(tok.pubkey(),True),meta(self.mint.pubkey())])
    @property
    def slot(self):return self.vm.get_clock().slot
    def warp(self,slot):self.vm.warp_to_slot(slot)
    def send(self,ixs,signers=(),name=None,error=None):
        keys={str(k.pubkey()):k for k in [self.payer,*signers]}
        msg=Message.new_with_blockhash([set_compute_unit_limit(getattr(self,'compute_budget',1_000_000)),*ixs],self.payer.pubkey(),self.vm.latest_blockhash())
        tx=VersionedTransaction(msg,list(keys.values()))
        result=self.vm.send_transaction(tx)
        self.vm.expire_blockhash()
        if error is None:
            assert isinstance(result,TransactionMetadata),(name,result)
            if name:MEASUREMENTS.append({'scenario':name,'cu':result.compute_units_consumed()})
        else:
            assert isinstance(result,FailedTransactionMetadata),(name,result)
            if type(error) is int:assert f'Custom({error})' in str(result.err()),(error,result)
        return result
    def oracle(self,p):
        return self.send([command(self.program,self.market.pubkey(),self.payer.pubkey(),1,payload=struct.pack('<QI',p,10))],name='oracle')
    def call(self,s,op,payload=b'',name=None,error=None,signer=None,seq=None,epoch=None,extras=None,deadline=None):
        if seq is None:self.seq[s]+=1;seq=self.seq[s]
        signer=signer or self.actors[s]
        if extras is None:extras=([meta(self.vault.pubkey(),True),meta(self.tokens[s].pubkey(),True),meta(self.auth),meta(TOKEN)] if op in (3,4) else [])
        ix=command(self.program,self.market.pubkey(),signer.pubkey(),op,s,payload,seq=seq,epoch=epoch or self.epoch[s],extras=extras,deadline=self.slot+150 if deadline is None else deadline)
        before=self.data()
        r=self.send([ix],[signer],name,error)
        if error is not None:assert before==self.data(),'failed instruction mutated market'
        return r
    def data(self):return self.vm.get_account(self.market.pubkey()).data
    def check(self):
        v=self.vm.get_account(self.vault.pubkey()).data
        return assert_conservation(self.data(),struct.unpack_from('<Q',v,64)[0])
    def quote(self,s,entries,name=None,error=None):
        return self.call(s,5,quotes([(k,p,x,self.slot+100,c,False) for c,(k,p,x) in enumerate(entries)]),name,error)
    def settle(self):return self.send([command(self.program,self.market.pubkey(),self.payer.pubkey(),10)],name='cash-settle-16-cap')

class EngineTests(unittest.TestCase):
    def test_roundtrip_accounting_funding_and_withdraw(self):
        l=Lab();l.quote(0,[(0,14999,10000),(8,15001,10000)],'replace-1+1')
        l.call(1,6,ioc(0,10000,15001,minimum=10000),name='ioc-1-maker')
        st=l.check();self.assertEqual([s['position'] for s in st['seats']],[-10000,10000,0]);self.assertEqual(st['fees'],75005)
        l.warp(l.slot+1000);l.oracle(15100)
        l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),11,payload=struct.pack('<q',2))],name='funding')
        st=l.check();self.assertEqual(st['seats'][0]['funding_adjusted_quote'],150030000)
        l.settle();l.check()
        l.quote(0,[(0,15100,10000)])
        l.call(1,6,ioc(1,10000,15100,minimum=10000,reduce=True),name='reduce-close')
        st=l.check();self.assertEqual(sum(abs(s['position']) for s in st['seats']),0)
        l.settle();st=l.check()
        for s in range(3):l.call(s,4,struct.pack('<Q',st['seats'][s]['collateral']),name='withdraw')
        self.assertEqual(l.check()['fees'],150505)
    def test_priority_partial_and_atomic_frame(self):
        l=Lab();l.quote(0,[(8,15001,100)]);l.quote(2,[(8,15001,100)])
        l.quote(0,[(8,15001,50)],'same-price-reduce')
        l.call(1,6,ioc(0,60,15001,minimum=60),name='ioc-two-makers')
        st=l.check();self.assertEqual([s['position'] for s in st['seats']],[-50,60,-10])
        before=l.data();l.quote(0,[(0,15002,10),(8,15001,10)],error=21);self.assertEqual(before,l.data())
    def test_replay_auth_and_margin(self):
        l=Lab();p=quotes([(0,14990,100,l.slot+100,123,False)])
        l.call(0,5,p,name='replace-first');seq=l.seq[0];before=l.data()
        l.call(0,5,p,seq=seq,name='idempotent-retry');self.assertEqual(before,l.data())
        l.call(0,7,struct.pack('<Q',65535),seq=seq,error=44)
        l.call(0,7,struct.pack('<Q',65535),seq=seq-1,error=43)
        l.call(0,7,struct.pack('<Q',65535),signer=l.actors[1],error=8)
        l.call(0,4,struct.pack('<Q',1_000_000_000),error=12)
        l.quote(0,[(0,14990,1_000_000)],error=12)
        l.call(0,7,struct.pack('<Q',1),name='cancel-1');l.check()
    def test_stale_oracle_cancel_session_revocation(self):
        l=Lab();l.quote(0,[(0,14999,100)])
        delegate=Keypair();l.call(0,8,bytes(delegate.pubkey())+struct.pack('<Q',l.slot+200));l.epoch[0]+=1
        l.call(0,5,quotes([(0,14999,100,l.slot+100,0,False)]),signer=delegate,name='delegated-quote')
        l.call(0,4,struct.pack('<Q',1),signer=delegate,error=8)
        l.call(0,8,bytes(l.actors[0].pubkey())+struct.pack('<Q',l.slot+200));l.epoch[0]+=1
        l.call(0,7,struct.pack('<Q',65535),signer=delegate,error=8)
        l.warp(l.slot+151);l.quote(0,[(0,14999,100)],error=10)
        l.call(0,7,struct.pack('<Q',65535),name='cancel-stale-oracle');l.check()
    def test_cleanup_visit_and_min_fill_rollback(self):
        l=Lab();l.quote(0,[(8,15000,100)])
        l.call(2,5,quotes([(8,15001,100,l.slot+150,0,False)]))
        l.warp(l.slot+101);l.oracle(15000)
        l.call(1,6,ioc(0,100,15001,visits=1,minimum=1),error=24)
        l.call(1,6,ioc(0,100,15001,visits=1),name='expired-visit-only')
        self.assertEqual(l.check()['seats'][1]['position'],0)
        l.call(1,6,ioc(0,100,15001,minimum=100),name='after-expired-cleanup');l.check()
    def test_replace_eight_and_ioc_eight(self):
        l=Lab();l.quote(0,[(i,14990-i,100) for i in range(4)]+[(8+i,15010+i,100) for i in range(4)],'replace-4+4')
        l.quote(0,[(8+i,15000+i,100) for i in range(8)],'replace-8-asks')
        l.call(1,6,ioc(0,800,15007,minimum=800,visits=8),name='ioc-8-orders-one-maker');l.check()
    def test_eight_distinct_maker_seats(self):
        l=Lab(9)
        for maker in range(8):l.quote(maker,[(8,15000+maker,100)])
        l.call(8,6,ioc(0,800,15007,minimum=800,visits=8),name='ioc-8-maker-seats')
        st=l.check();self.assertEqual([s['position'] for s in st['seats']],[-100]*8+[800])

    def test_absolute_price_bitmap_boundaries(self):
        for price in (1,63,64,255,256,65535,65536,0xffffff):
            l=Lab();l.oracle(price);l.quote(0,[(8,price,1)])
            l.call(1,6,ioc(0,1,price,minimum=1))
            self.assertEqual(l.check()['seats'][1]['quote'],-price)

    def test_sparse_page_capacity_recycles(self):
        actors=PAGES//8+1
        l=Lab(actors);l.oracle(1000000)
        for seat in range(actors-1):
            l.quote(seat,[(8+k,1000100+(seat*8+k)*256,1) for k in range(8)])
        next_price=1000100+PAGES*256
        l.quote(actors-1,[(8,next_price,1)],error=22)
        l.call(0,7,struct.pack('<Q',65535))
        l.quote(actors-1,[(8,next_price,1)])
        self.assertEqual(len(l.check()['orders']),PAGES-8+1)

    def test_seeded_reference_book_and_ledger(self):
        import random
        l=Lab(8);rng=random.Random(20261007);orders={};priority=0
        positions=[0]*8;cash=[0]*8;coll=[1_000_000_000]*8;fees=0
        for step in range(400):
            who=rng.randrange(8);action=rng.randrange(3)
            if action==0:
                entries=[]
                for k in [0,1,8,9]:
                    if rng.randrange(3):entries.append((k,14990-rng.randrange(4) if k<8 else 15010+rng.randrange(4),rng.randrange(1,101)))
                present={k for k,_,_ in entries}
                for key in list(orders):
                    if key[0]==who and key[1] not in present:del orders[key]
                for k,p,x in entries:
                    old=orders.get((who,k));priority+=1
                    rank=old[2] if old and old[0]==p and x<=old[1] else priority
                    orders[who,k]=(p,x,rank)
                l.quote(who,entries)
            elif action==1:
                mask=rng.randrange(65536)
                l.call(who,7,struct.pack('<Q',mask))
                for key in list(orders):
                    if key[0]==who and mask&(1<<key[1]):del orders[key]
            else:
                side=rng.randrange(2);remaining=rng.randrange(1,501);visits=rng.randrange(1,17)
                l.call(who,6,ioc(side,remaining,15100 if side==0 else 14900,visits=visits))
                for _ in range(visits):
                    eligible=[(key,v) for key,v in orders.items() if key[1]//8!=side]
                    if not eligible or not remaining:break
                    key,(p,x,rank)=min(eligible,key=lambda kv:((kv[1][0] if side==0 else -kv[1][0]),kv[1][2]))
                    maker=key[0]
                    if maker==who:del orders[key];continue
                    take=min(x,remaining);sign=1 if side==0 else -1
                    positions[who]+=sign*take;positions[maker]-=sign*take
                    cash[who]-=sign*take*p;cash[maker]+=sign*take*p
                    fee=(take*p*5+9999)//10000;coll[who]-=fee;fees+=fee
                    if x==take:del orders[key]
                    else:orders[key]=(p,x-take,rank)
                    remaining-=take
            st=l.check()
            self.assertEqual([x['position'] for x in st['seats']],positions,step)
            self.assertEqual([x['quote'] for x in st['seats']],cash,step)
            self.assertEqual([x['collateral'] for x in st['seats']],coll,step)
            self.assertEqual(st['fees'],fees,step)
            self.assertEqual({(x['seat'],x['slot']):(x['price'],x['lots']) for x in st['orders']},{key:(v[0],v[1]) for key,v in orders.items()},step)
        l.settle();l.check()

    def test_token_substitution_and_malformed_packets(self):
        l=Lab()
        wrong=[meta(l.tokens[1].pubkey(),True),meta(l.tokens[0].pubkey(),True),meta(l.auth),meta(TOKEN)]
        l.call(0,3,struct.pack('<Q',1),extras=wrong,error=30)
        l.call(0,6,ioc(0,2**64-1,15000),error=23)
        l.call(0,5,quotes([(0,14990,10,l.slot+100,0,False),(0,14990,10,l.slot+100,0,False)]),error=47)
        before=l.data()
        for packet in (b'',bytes(47),bytes(48),b'\1'+bytes(90)):
            l.send([Instruction(l.program,packet,[meta(l.market.pubkey(),True),meta(l.actors[0].pubkey(),False,True)])],[l.actors[0]],error=True)
            self.assertEqual(l.data(),before)
        l.check()

    def test_liquidation_and_bad_debt_halt(self):
        l=Lab();l.call(1,4,struct.pack('<Q',970_000_000));l.call(2,4,struct.pack('<Q',970_000_000))
        l.quote(0,[(8,15000,10000)]);l.call(1,6,ioc(0,10000,15000,minimum=10000))
        l.oracle(12500)
        l.call(2,12,struct.pack('<H6xQ',1,10000),name='liquidation-takeover')
        st=l.check();self.assertEqual(st['seats'][1]['position'],0);self.assertEqual(st['seats'][2]['position'],10000)
        l.settle();l.check()
        # Unfunded bad debt is blocked instead of creating withdrawable claims.
        l.oracle(1)
        l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),10)],error=52)
        l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),14,2)],name='resolution-halt')
        self.assertTrue(l.check()['halted'])
        l.quote(0,[(0,1,1)],error=46)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(EngineTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    target=Path(__file__).resolve().parents[2]/'evidence/sbf-cu.json';target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({'elf':str(ELF),'measurements':MEASUREMENTS,'tests':result.testsRun,'success':result.wasSuccessful()},indent=2)+'\n')
    sys.exit(not result.wasSuccessful())
