import struct,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_sbf import Lab
from wire import *
from solders.account import Account
from solders.keypair import Keypair
PYTH=Pubkey.from_string('7AviUf9nL62mcxNbQGKm4nKDQnPjswo6c5MX4D57HmyE')
RECEIVER=Pubkey.from_string('rec2HHDDnjLfj4kE7VyEtFA1HPGQLK33259532cRyHp')
FEED=bytes.fromhex('ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d')
def fixture(l,now=1_790_000_000,price=15_000_000_000,conf=10000,level=1,feed=FEED,owner=RECEIVER,publish=None):
    clock=l.vm.get_clock();clock.unix_timestamp=now;l.vm.set_clock(clock)
    b=bytes.fromhex('22f123639d7ef4cd')+bytes(32)+bytes([level])+feed+struct.pack('<qQiqqqQQ',price,conf,-8,publish if publish is not None else now,now-1,price,conf,l.slot)+b'\0'
    l.vm.set_account(PYTH,Account(10_000_000,b,owner))
def op(l,code,error=None):
    return l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),code,extras=[meta(PYTH)] if code in (15,16) else [])],name='pyth-'+str(code),error=error)
def enable(l):
    for s in range(3):l.call(s,4,struct.pack('<Q',l.check()['seats'][s]['collateral']))
    fixture(l);op(l,15)
    for s in range(3):l.call(s,3,struct.pack('<Q',1_000_000_000))
class OracleSessions(unittest.TestCase):
    def test_pinned_full_price_and_no_admin_override(self):
        l=Lab();enable(l);self.assertEqual(l.check()['oracle'],15000)
        l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),1,payload=struct.pack('<QI',16000,0))],error=55)
        l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),11,payload=struct.pack('<q',1))],error=55)
        fixture(l,level=0);op(l,16,55)
        fixture(l,feed=bytes(32));op(l,16,55)
        fixture(l,owner=Pubkey.new_unique());op(l,16,55)
        fixture(l,publish=1_790_000_001);op(l,16,56)
        fixture(l,conf=500_000_000);op(l,16,57)
        fixture(l,now=1_790_000_100,publish=1_790_000_000);op(l,16,56)
        l.quote(0,[(0,14999,1)],error=56)
        l.call(0,7,struct.pack('<Q',65535))
    def test_sampling_gap_blocks_funding_and_valid_history_advances(self):
        l=Lab();enable(l)
        op(l,17,58)
        for i in range(1,22):fixture(l,now=1_790_000_000+15*i);op(l,16)
        fixture(l,now=1_790_000_320);op(l,17)
        self.assertEqual(l.check()['funding_updates'],1);self.assertEqual(l.check()['funding'],0)
        fixture(l,now=1_790_000_325);op(l,16)
        self.assertEqual(l.check()['funding_observed_seconds'],5)
        fixture(l,now=1_790_000_400);op(l,16)
        self.assertEqual(l.check()['funding_observed_seconds'],0);op(l,17,58)
        # Deterministic nonzero premium and tick value exercise exact carry/ledger.
        l.quote(0,[(0,15012,1),(8,15014,1)])
        start=1_790_000_400
        for i in range(1,194):fixture(l,now=start+15*i);op(l,16)
        op(l,17);self.assertGreater(l.check()['funding'],0);l.check()
    def test_scoped_session_limits_expiry_and_turnover(self):
        l=Lab();enable(l);session=Keypair()
        payload=bytes(session.pubkey())+struct.pack('<QQQQ',l.slot+20,(1<<5)|(1<<7),2,15001)
        l.call(0,8,payload);l.epoch[0]+=1;l.seq[0]=0
        l.call(0,5,quotes([(8,15001,2,l.slot+10,1,False)],1_790_000_020),signer=session)
        l.call(0,5,quotes([(8,15001,3,l.slot+10,1,False)],1_790_000_020),signer=session,error=59)
        l.call(0,5,quotes([(8,15001,2,l.slot+21,1,False)],1_790_000_020),signer=session,error=59)
        l.call(0,6,ioc(0,1,15010),signer=session,error=59)
        l.call(0,4,struct.pack('<Q',1),signer=session,error=8)
        l.call(1,6,ioc(0,2,15001))
        st=l.check();self.assertEqual(st['seats'][0]['position'],-1);self.assertEqual(st['seats'][0]['session_turnover_remaining'],0)
        l.call(0,8,bytes(l.actors[0].pubkey())+struct.pack('<Q',l.slot+20));l.epoch[0]+=1;l.seq[0]=0
        l.call(0,7,struct.pack('<Q',65535),signer=session,error=8)
    def test_policy_expiry_prevents_fill_and_expired_top_does_not_block_quotes(self):
        l=Lab();enable(l)
        l.call(0,5,quotes([(8,15001,2,l.slot+100,1,False)],1_790_000_010))
        fixture(l,now=1_790_000_010)
        l.call(1,6,ioc(0,2,15001));self.assertEqual(l.check()['seats'][1]['position'],0)
        l.call(0,5,quotes([(8,15001,2,l.slot+100,1,False)],1_790_000_050),error=60)
        l.call(0,5,quotes([(8,15001,2,l.slot+100,1,False)],1_790_000_011))
        fixture(l,now=1_790_000_012)
        l.quote(1,[(0,15002,1)]);self.assertFalse(any(o['seat']==0 for o in l.check()['orders']))
    def test_limited_session_required_in_external_mode(self):
        l=Lab();enable(l);session=Keypair()
        l.call(0,8,bytes(session.pubkey())+struct.pack('<Q',l.slot+20),error=59)
        bad=bytes(session.pubkey())+struct.pack('<QQQQ',l.slot+20,1<<4,2,10000)
        l.call(0,8,bad,error=59)
    def test_session_ioc_policy_deadline_is_enforced_onchain(self):
        l=Lab();enable(l);session=Keypair()
        l.call(1,8,bytes(session.pubkey())+struct.pack('<QQQQ',l.slot+20,(1<<6)|(1<<7),2,100000))
        l.epoch[1]+=1;l.seq[1]=0
        l.quote(0,[(8,15001,2)])
        l.call(1,6,ioc(0,2,15001),signer=session,error=60)
        l.call(1,6,ioc(0,2,15001,policy_until=1_790_000_010),signer=session)
        fixture(l,now=1_790_000_010)
        l.call(1,6,ioc(0,2,15001,policy_until=1_790_000_010),signer=session,error=60)
if __name__=='__main__':unittest.main(verbosity=2)
