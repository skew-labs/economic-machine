import struct,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_sbf import Lab
from test_oracle_sessions import enable,fixture,op
from wire import *
from solders.keypair import Keypair

def donate(l,s,amount):
    l.call(s,4,struct.pack('<Q',amount))
    extras=[meta(l.vault.pubkey(),True),meta(l.tokens[s].pubkey(),True),meta(TOKEN)]
    return l.send([command(l.program,l.market.pubkey(),l.actors[s].pubkey(),18,payload=struct.pack('<Q',amount),extras=extras)],[l.actors[s]],'insurance-donation')
def resolve(l,error=None):
    return l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),19)],name='terminal-adl',error=error)
def crash(l,price=1):
    l.quote(0,[(8,15000,100000)]);l.call(1,6,ioc(0,100000,15000,minimum=100000));l.oracle(price)

class Resolution(unittest.TestCase):
    def test_insurance_donation_and_deficit_waterfall(self):
        l=Lab();donate(l,2,600_000_000);self.assertEqual(l.check()['insurance'],600_000_000)
        crash(l);before=l.check();deficit=-before['seats'][1]['equity'];self.assertGreater(deficit,0)
        # Insurance may not reset an underwater open position's cost basis.
        before_bytes=l.data();l.send([command(l.program,l.market.pubkey(),l.payer.pubkey(),10)],error=52)
        self.assertEqual(l.data(),before_bytes)
        l.call(2,12,struct.pack('<H6xQ',1,100000));self.assertEqual(l.check()['seats'][1]['position'],0)
        l.settle();st=l.check();self.assertEqual(st['insurance_spent'],deficit)
        self.assertEqual(st['insurance'],600_000_000-deficit);self.assertEqual(st['fees'],before['fees'])
        self.assertFalse(st['resolved']);resolve(l,62)
    def test_terminal_adl_only_haircuts_unsettled_profit_and_allows_exit(self):
        l=Lab();donate(l,2,100_000_000);crash(l);before=l.check()
        deficit=-before['seats'][1]['equity'];loss=deficit-before['insurance']-before['fees']
        self.assertGreater(loss,0);result=resolve(l);self.assertLess(result.compute_units_consumed(),1_000_000)
        st=l.check();self.assertTrue(st['resolved'] and st['halted']);self.assertEqual(st['adl_loss'],loss)
        self.assertEqual(st['seats'][0]['collateral'],before['seats'][0]['equity']-loss)
        self.assertEqual(st['seats'][2]['collateral'],before['seats'][2]['collateral'])
        self.assertEqual(st['insurance'],0);self.assertEqual(st['fees'],0)
        self.assertTrue(all(s['position']==s['quote']==0 and s['session']==s['owner'] for s in st['seats']))
        for s in range(3):
            l.epoch[s]=st['seats'][s]['epoch'];l.seq[s]=0
            if st['seats'][s]['collateral']:l.call(s,4,struct.pack('<Q',st['seats'][s]['collateral']))
        self.assertEqual(sum(s['collateral'] for s in l.check()['seats']),0)
        resolve(l,61);l.quote(0,[(0,1,1)],error=46);l.call(0,3,struct.pack('<Q',1),error=46)
    def test_solvent_or_stale_resolution_rejected_atomically(self):
        l=Lab();before=l.data();resolve(l,62);self.assertEqual(l.data(),before)
        crash(l);l.warp(l.slot+151);before=l.data();resolve(l,10);self.assertEqual(l.data(),before)
    def test_sixteen_seat_liquidation_and_adl_rounding(self):
        l=Lab(16)
        for s in range(0,16,2):
            l.quote(s,[(8,15000,100000+s)]);l.call(s+1,6,ioc(0,100000+s,15000,minimum=100000+s))
        l.oracle(1);before=l.check();self.assertEqual(sum(s['equity']<0 for s in before['seats']),8)
        result=resolve(l);self.assertLess(result.compute_units_consumed(),1_000_000)
        st=l.check();self.assertEqual(st['resolutions'],1);self.assertFalse(st['orders'])
        loss=sum(max(0,-s['equity']) for s in before['seats'])-before['fees']
        profit=sum(max(0,s['equity']-s['collateral']) for s in before['seats'])
        charged=0
        for old,new in zip(before['seats'],st['seats']):
            if old['equity']<0:self.assertEqual(new['collateral'],0)
            else:
                cut=old['equity']-new['collateral'];charged+=cut
                floor=max(0,old['equity']-old['collateral'])*loss//profit
                self.assertIn(cut,(floor,floor+1));self.assertGreaterEqual(new['collateral'],old['collateral'])
        self.assertEqual(charged,loss)
    def test_independent_liquidators_across_many_victims(self):
        l=Lab(16)
        for s in range(0,12,2):l.quote(s,[(8,15000,100000)]);l.call(s+1,6,ioc(0,100000,15000,minimum=100000))
        l.oracle(5000)
        for n,s in enumerate(range(1,12,2)):
            before=l.check();self.assertLess(before['seats'][s]['equity'],25_000_000)
            l.call(12+n%4,12,struct.pack('<H6xQ',s,100000),name='mass-liquidation')
            self.assertEqual(l.check()['seats'][s]['position'],0)
        l.check()
    def test_external_oracle_required_for_adl(self):
        l=Lab();enable(l);l.quote(0,[(8,15000,100000)]);l.call(1,6,ioc(0,100000,15000,minimum=100000))
        fixture(l,price=1_000_000,conf=1);op(l,16);resolve(l);self.assertTrue(l.check()['resolved'])
    def test_insurance_cannot_spend_someone_elses_tokens(self):
        l=Lab();l.call(2,4,struct.pack('<Q',100))
        extras=[meta(l.vault.pubkey(),True),meta(l.tokens[2].pubkey(),True),meta(TOKEN)]
        before=l.data();l.send([command(l.program,l.market.pubkey(),l.actors[0].pubkey(),18,payload=struct.pack('<Q',100),extras=extras)],[l.actors[0]],error=30)
        self.assertEqual(l.data(),before);l.check()
if __name__=='__main__':unittest.main(verbosity=2)
