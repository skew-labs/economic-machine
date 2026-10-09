import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agents'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
from feed_evidence import recovery_trades
from rt_net import socket_fault_until
from rt_store import Store

def event(kind,t,**body):return {'kind':kind,'unix_ns':t,'role':-1,'body':dict(monotonic_ns=t,**body)}
def tx(role,op,t,filled=0):return {'role':role,'signature':str((role,op,t)),'state':'CONFIRMED','body':{'op':op,'source_ns':t,'submit_ns':t+1,'confirmed_ns':t+2,'filled_lots':filled}}
EVENTS=[event('feed_connected',1),event('feed_disconnect_injected',10),event('feed_http_recovered',20,source_ns=19),event('feed_retry_rejected_injected',30),event('feed_connected',50)]
TXS=[tx(role,op,t,2) for t in (25,55) for role,op in ((0,5),(1,5),(2,6))]
class RecoveryEvidence(unittest.TestCase):
    def test_actual_quotes_and_fills_required_in_both_windows(self):
        r=recovery_trades(EVENTS,TXS);self.assertTrue(r['passed']);self.assertEqual(r['http_fallback']['filled_lots'],2)
        self.assertFalse(recovery_trades(EVENTS,[t for t in TXS if t['role']!=1])['passed'])
        zero=[dict(t,body=dict(t['body'],filled_lots=0)) for t in TXS]
        self.assertFalse(recovery_trades(EVENTS,zero)['passed'])
    def test_pre_outage_source_and_late_receipts_cannot_prove_fallback(self):
        old=[dict(t,body=dict(t['body'],source_ns=15)) for t in TXS]
        self.assertFalse(recovery_trades(EVENTS,old)['http_fallback']['passed'])
        late=[dict(t,body=dict(t['body'],confirmed_ns=60)) for t in TXS]
        self.assertFalse(recovery_trades(EVENTS,late)['http_fallback']['passed'])
        self.assertFalse(recovery_trades(EVENTS[:-1],TXS)['passed'])
    def test_fault_deadline_survives_restart_without_extension(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'runtime.sqlite';m={'fault_drill':True,'feed_retry_fault_seconds':25}
            s=Store(p,m);self.assertEqual(socket_fault_until(s,m),0)
            s.event(-1,'feed_disconnect_injected',{'retry_fault_until_unix':1234});s.db.close()
            s=Store(p,m);self.assertEqual(socket_fault_until(s,m),1234);s.db.close()
    def test_fault_is_bounded_and_only_available_in_drill(self):
        for m in ({'fault_drill':False,'feed_retry_fault_seconds':25},{'fault_drill':True,'feed_retry_fault_seconds':31},{'fault_drill':True,'feed_retry_fault_seconds':True}):
            with self.assertRaises(RuntimeError):socket_fault_until(None,m)
if __name__=='__main__':unittest.main(verbosity=2)
