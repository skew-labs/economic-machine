"""Real production ELF; receiver accounts are explicit test fixtures, not attestations."""
import struct,sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1]/'agents')]
from test_sbf import Lab
from test_oracle_sessions import fixture,PYTH,RECEIVER
from wire import *
from solders.instruction import Instruction
from solders.account import Account
from solders.transaction_metadata import FailedTransactionMetadata

class InitRejected(Exception):pass

class ProductionLab(Lab):
    def __init__(self,*args,feed=None,fixture_args=None,init_error=None,omit_feed=False,**kwargs):
        self.feed=feed or PYTH;self.fixture_args=fixture_args or {};self.init_error=init_error;self.omit_feed=omit_feed
        super().__init__(*args,**kwargs)
    def send(self,ixs,signers=(),name=None,error=None):
        if name=='initialize':
            fixture(self,**self.fixture_args)
            if self.feed!=PYTH:self.vm.set_account(self.feed,self.vm.get_account(PYTH))
            ix=ixs[0]
            if not self.omit_feed:
                tick_value=struct.unpack_from('<Q',ix.data,49)[0]
                ixs=[initialize(self.program,self.market.pubkey(),self.payer.pubkey(),self.mint.pubkey(),self.vault.pubkey(),tick_value,feed=self.feed)]
            if self.init_error is not None:
                before=self.data();super().send(ixs,signers,name,self.init_error)
                assert self.data()==before,'rejected initialization persisted a header'
                raise InitRejected()
        return super().send(ixs,signers,name,error)
    def oracle(self,p):
        # Lab initializes a fixture price after initialization. Production must
        # already be fully priced by initialize; subsequent changes use op16.
        if not hasattr(self,'initial_oracle_seen'):
            self.initial_oracle_seen=True;assert self.check()['oracle']==p;return
        now=self.vm.get_clock().unix_timestamp
        fixture(self,now=now,price=p*1_000_000)
        if self.feed!=PYTH:self.vm.set_account(self.feed,self.vm.get_account(PYTH))
        return self.send([command(self.program,self.market.pubkey(),self.payer.pubkey(),16,extras=[meta(self.feed)])],name='production-oracle')

class ProductionInit(unittest.TestCase):
    def test_external_price_and_pin_exist_before_first_deposit(self):
        l=ProductionLab();st=l.check()
        self.assertEqual(l.data()[:8],b'MPERPS03');self.assertEqual(st['oracle_mode'],1)
        self.assertEqual(st['oracle'],15000);self.assertEqual(st['pinned_oracle_account'],str(PYTH))
        self.assertGreater(st['oracle_publish_time'],0)
    def test_legacy_init_is_rejected_without_partial_header(self):
        with self.assertRaises(InitRejected):ProductionLab(omit_feed=True,init_error=4)
        k=Pubkey.new_unique()
        with self.assertRaises(ValueError):initialize(k,k,k,k,k,1)
    def test_bad_receiver_and_partial_verification_are_atomic(self):
        for args in ({'owner':Pubkey.new_unique()},{'level':0},{'feed':bytes(32)}):
            with self.subTest(args=list(args)),self.assertRaises(InitRejected):ProductionLab(fixture_args=args,init_error=55)
    def test_stale_future_and_low_confidence_initialization(self):
        for args,error in (({'publish':1_789_999_900},56),({'publish':1_790_000_001},56),({'conf':500_000_000},57)):
            with self.subTest(args=args),self.assertRaises(InitRejected):ProductionLab(fixture_args=args,init_error=error)
    def test_network_specific_account_is_selected_once(self):
        chosen=Pubkey.new_unique();l=ProductionLab(feed=chosen)
        self.assertEqual(l.check()['pinned_oracle_account'],str(chosen))
        l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),16,extras=[meta(PYTH)])],error=55)
        l.oracle(15100);self.assertEqual(l.check()['oracle'],15100)
    def test_admin_oracle_funding_and_mode_switch_are_disabled(self):
        l=ProductionLab()
        for op,payload,extras in ((1,struct.pack('<QI',16000,0),[]),(11,struct.pack('<q',1),[]),(15,b'',[meta(PYTH)])):
            before=l.data();l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),op,payload=payload,extras=extras)],error=55)
            self.assertEqual(before,l.data())
    def test_stale_feed_blocks_quote_but_allows_cancel(self):
        l=ProductionLab();l.quote(0,[(8,15001,1)])
        clock=l.vm.get_clock();clock.unix_timestamp+=91;l.vm.set_clock(clock)
        l.quote(0,[(8,15001,1)],error=56);l.call(0,7,struct.pack('<Q',65535));l.check()
    def test_old_market_magic_cannot_be_used_by_release(self):
        l=ProductionLab();account=l.vm.get_account(l.market.pubkey());d=bytearray(account.data);d[:8]=b'MPERPS02'
        l.vm.set_account(l.market.pubkey(),Account(account.lamports,bytes(d),account.owner))
        l.call(0,7,struct.pack('<Q',65535),error=5)
    def test_settle_close_and_withdraw_with_external_feed(self):
        l=ProductionLab();l.quote(0,[(8,15001,100)])
        l.call(1,6,ioc(0,100,15001,minimum=100));self.assertEqual(l.check()['seats'][1]['position'],100)
        l.oracle(15100);l.quote(0,[(0,15100,100)]);l.call(1,6,ioc(1,100,15100,minimum=100,reduce=True))
        l.settle();st=l.check();self.assertEqual(sum(abs(s['position']) for s in st['seats']),0)
        for s in range(3):l.call(s,4,struct.pack('<Q',st['seats'][s]['collateral']))
        st=l.check();self.assertEqual(sum(s['collateral'] for s in st['seats']),0)
    def test_actual_economic_machine_decodes_release_layout(self):
        from feed_batch import Batch,Input,INPUT_BYTES
        l=ProductionLab();l.quote(0,[(0,14999,2),(8,15001,3)])
        facts=Batch().prepare(l.data(),l.slot,100,101,l.vm.get_clock().unix_timestamp)
        x=Input.from_buffer_copy(facts[INPUT_BYTES:2*INPUT_BYTES])
        self.assertEqual((x.mark,x.bid,x.ask,x.depth),(15000,14999,15001,5))

if __name__=='__main__':unittest.main(verbosity=2)
