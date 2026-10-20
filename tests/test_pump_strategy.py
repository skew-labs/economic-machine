import base64
import json
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from economic_machine.pump_research.events import TRADE,PREFIX,FEES,VOLUME,QUOTE,trade,scoped_logs,valuation_reason,ZERO
from economic_machine.pump_research.store import PUMP,encode,decode,Store,canonical
from economic_machine.pump_research.strategy import CONFIG,features,signal,evaluate,buy_quote,sell_quote,fees
from economic_machine.pump_research.shadow import schema,protocol,population

def key(n):return encode(bytes([n])*32)
def event(slot=101,user=None,buy=True):
    return dict(mint=key(1),user=user or key(2),sol_amount=200000000,token_amount=100000000,
        is_buy=buy,timestamp=1,virtual_sol_reserves=40000000000,virtual_token_reserves=800000000000000,
        real_sol_reserves=10000000000,real_token_reserves=500000000000000,fee_recipient=key(3),
        fee_basis_points=95,fee=1900000,creator=key(4),creator_fee_basis_points=30,creator_fee=600000,
        track_volume=False,total_unclaimed_tokens=0,total_claimed_tokens=0,current_sol_volume=0,
        last_update_timestamp=1,ix_name='buy' if buy else 'sell',mayhem_mode=False,
        cashback_fee_basis_points=0,cashback=0,buyback_fee_basis_points=0,buyback_fee=0,
        shareholders=[],quote_mint=ZERO,quote_amount=200000000,virtual_quote_reserves=40000000000,
        real_quote_reserves=10000000000,holder_rewards_bps=0,holder_rewards=0,creator_fee_unclaimed=0,
        layout_supported=True,slot=slot,tx_order=0,ix=0,signature=str(slot)+str(user),observed=slot)
def pack(e):
    raw=TRADE
    for fields in (PREFIX,FEES,VOLUME,QUOTE):
        if fields is QUOTE:raw+=struct.pack('<I',0)
        for name,t in fields:
            v=e[name]
            if t=='pubkey':raw+=decode(v)
            elif t=='string':raw+=struct.pack('<I',len(v.encode()))+v.encode()
            else:raw+=int(v).to_bytes({'u64':8,'i64':8,'u16':2,'bool':1}[t],'little',signed=t=='i64')
    return raw
def launch():return dict(slot=100,tx_order=0,ix=0,payer=key(4),creator=key(4),observed=100)

class StrategyTests(unittest.TestCase):
    def test_current_event_integer_layout(self):
        e=event();e['token_amount']=2**63+19
        out=trade(pack(e));self.assertEqual(out['token_amount'],2**63+19);self.assertTrue(out['layout_supported'])
    def test_unknown_tail_never_priced(self):
        e=trade(pack(event())+b'12345678');self.assertEqual(valuation_reason(e),'unsupported_event_layout')
    def test_legacy_fees_not_invented(self):
        e=trade(pack(event())[:129]);self.assertFalse(e['layout_supported']);self.assertNotIn('fee_basis_points',e)
    def test_truncated_event_rejected(self):
        with self.assertRaises(ValueError):trade(pack(event())[:-1])
    def test_noncanonical_boolean_rejected(self):
        raw=bytearray(pack(event()));raw[56]=2
        with self.assertRaises(ValueError):trade(bytes(raw))
    def test_router_spoof_ignored(self):
        m=NS(log_messages=[f'Program {key(8)} invoke [1]','Program data: '+base64.b64encode(pack(event())).decode(),f'Program {key(8)} success'])
        self.assertEqual(scoped_logs(m,[]),([],[]))
    def test_event_must_match_executed_instruction(self):
        m=NS(log_messages=[f'Program {PUMP} invoke [1]','Program data: '+base64.b64encode(pack(event())).decode(),f'Program {PUMP} success'])
        self.assertEqual(scoped_logs(m,[])[1],['EVENT_INSTRUCTION_MISMATCH'])
        a=[dict(mint=key(1),wallet=key(2),action='buy',ix=7)]
        out,errors=scoped_logs(m,a);self.assertEqual(errors,[]);self.assertEqual(out[0]['ix'],7)
    def test_truncated_stack_cannot_validate(self):
        m=NS(log_messages=[f'Program {PUMP} invoke [1]','Program data: '+base64.b64encode(pack(event())).decode()])
        out,errors=scoped_logs(m,[dict(mint=key(1),wallet=key(2),action='buy',ix=0)])
        self.assertEqual(out,[]);self.assertIn('INCOMPLETE_INVOCATION_STACK',errors)
    def test_quote_assets_modes_and_negative_unsigned_rejected(self):
        for field,value in [('quote_mint',key(7)),('mayhem_mode',True),('virtual_sol_reserves',2**64-10),('holder_rewards_bps',100),('buyback_fee_basis_points',10001)]:
            e=event();e[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):buy_quote(e,50000000)
    def test_buyback_not_charged_twice(self):
        e=event();a=buy_quote(e,50000000);e['buyback_fee_basis_points']=5000;e['buyback_fee']=e['fee']//2
        self.assertEqual(a,buy_quote(e,50000000))
    def test_fee_arithmetic_mismatch_rejected(self):
        e=event();e['fee']+=1
        with self.assertRaisesRegex(ValueError,'fee_arithmetic'):buy_quote(e,50000000)
    def test_price_impact_and_roundtrip_cost(self):
        e=event();small=buy_quote(e,50000000);large=buy_quote(e,100000000)
        self.assertLess(large,small*2);self.assertLess(sell_quote(e,small),50000000)
    def test_real_liquidity_not_virtual_capacity(self):
        e=event();e['real_sol_reserves']=e['real_quote_reserves']=1
        with self.assertRaisesRegex(ValueError,'insufficient_real_sol'):sell_quote(e,1000000)
    def test_migration_not_silently_clipped(self):
        e=event();e['real_token_reserves']=1
        with self.assertRaisesRegex(ValueError,'migration'):buy_quote(e,50000000)
    def test_fee_rounding_separate_ceil(self):self.assertEqual(fees(1,event()),2)
    def test_future_events_cannot_create_breadth(self):
        rows=[event(101)]+[event(112+i,key(i+10)) for i in range(10)]
        self.assertEqual(features(rows,launch(),112)['buyer_wallets'],1)
    def test_non_sol_amount_never_becomes_sol_demand(self):
        e=event();e['quote_mint']=key(8)
        self.assertEqual(features([e],launch(),112)['buy_lamports'],0)
    def test_same_slot_before_create_excluded(self):
        l=launch();l['tx_order']=10;e=event(100)
        self.assertEqual(features([e],l,112)['events'],0)
    def test_developer_sale_is_veto(self):
        rows=[event(101+i,key(10+i)) for i in range(6)]+[event(109,key(4),False)]
        self.assertEqual(signal(features(rows,launch(),112),CONFIG,'demand_filter'),'developer_selling')
    def test_shared_funder_is_association_veto(self):
        rows=[event(101+i,key(10+i)) for i in range(6)];links={key(10+i):key(50) for i in range(6)}
        f=features(rows,launch(),112,links);self.assertFalse(f['ownership_inferred'])
        self.assertEqual(signal(f,CONFIG,'demand_filter'),'shared_funding_concentration')
    def test_atomic_roundtrip_veto(self):
        rows=[event(101+i,key(10+i)) for i in range(6)]
        e=event(101,key(10),False);e['signature']=rows[0]['signature'];rows.append(e)
        self.assertEqual(signal(features(rows,launch(),112),CONFIG,'demand_filter'),'roundtrip_activity')
    def test_no_trade_launch_retained(self):
        out=evaluate([],launch(),500);self.assertEqual(out['status'],'no_entry');self.assertIsNone(out['validated_pnl_lamports'])
    def test_late_received_data_not_in_features(self):
        rows=[event(101+i,key(10+i)) for i in range(6)]+[event(114)]
        for e in rows[:6]:e['observed']=1000
        out=evaluate(rows,launch(),500);self.assertEqual(out['features']['buyer_wallets'],0)
    def test_missing_exit_not_zero_profit(self):
        rows=[event(101+i,key(10+i)) for i in range(6)]+[event(114)]
        out=evaluate(rows,launch(),500);self.assertEqual(out['status'],'exit_censored')
        self.assertNotIn('quote_return_lamports',out);self.assertLess(out['worst_case_loss_lamports'],0)
    def test_delayed_entry_and_exit(self):
        rows=[event(101+i,key(10+i)) for i in range(6)]+[event(114),event(264),event(266)]
        out=evaluate(rows,launch(),500);self.assertEqual(out['status'],'indicative_resolved')
        self.assertEqual(out['exit_slot'],266);self.assertLess(out['quote_return_lamports'],0)
    def test_exit_unsupported_not_cherry_picked(self):
        rows=[event(101+i,key(10+i)) for i in range(6)]+[event(114),event(115),event(266)]
        rows[-2]['mayhem_mode']=True
        self.assertEqual(evaluate(rows,launch(),500)['status'],'exit_censored')
    def test_no_winner_catalog_in_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(d,reserve=0);schema(s)
            with s.db:
                ev=s.evidence('ticking','x',b'x')
                tx=dict(signature='x',slot=200,index=0,success=True,transfers=[],activity=[],launches=[dict(ix=0,mint=key(1),payer=key(2),creator=key(2),name='No ATH',symbol='N')])
                s.ingest(tx,ev,'ticking','processed_unconfirmed')
            out=population(s,dict(registered_slot=150,registered_at=0,deadline=2**40))
            self.assertEqual(len(out),1);self.assertEqual(out[0]['split'],'prospective');s.db.close()
    def test_archive_transient_retries_preserve_scope_and_stop(self):
        from economic_machine.pump_research.cli import run_archive_job
        with tempfile.TemporaryDirectory() as d:
            s=Store(d,reserve=0)
            with s.db:
                s.set_state('archive_network_reserved',12345)
                s.db.execute('INSERT INTO jobs(id,kind,args,status,attempts,error) VALUES(?,?,?,?,?,?)',
                    ('job','slot_window',canonical(dict(start=300000000,end=300000000)),'failed',1,'HTTPError:HTTP Error 503: unavailable'))
            fake=NS(bytes=20,ingest_slot=lambda _:(_ for _ in ()).throw(HTTPError('public',503,'unavailable',{},None)))
            with patch('economic_machine.pump_research.cli.Archive',return_value=fake):
                with self.assertRaises(HTTPError):run_archive_job(s,None,100)
                self.assertEqual(s.db.execute('SELECT attempts FROM jobs').fetchone()[0],2)
                self.assertIsNone(run_archive_job(s,None,100)) # backoff
                with s.db:s.set_state('archive_retry_at:job',0)
                with self.assertRaises(HTTPError):run_archive_job(s,None,100)
                with s.db:s.set_state('archive_retry_at:job',0)
                self.assertIsNone(run_archive_job(s,None,100)) # three attempts total
            self.assertEqual(s.state('archive_network_reserved'),12345);s.db.close()
    def test_frozen_protocol_detects_parameter_edit(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(d,reserve=0)
            (Path(d)/'research-model-scope.json').write_text(canonical(dict(deadline_ms=int(time.time()*1000)+10000)))
            first=protocol(s,999);self.assertEqual(protocol(s,1000),first)
            self.assertEqual(first['registered_slot'],999)
            path=Path(d)/'strategy-protocol-v6.json';first['config']['minimum_buyers']=1
            path.write_text(canonical(first))
            with self.assertRaisesRegex(ValueError,'PROTOCOL_IDENTITY'):protocol(s)
            s.db.close()
    def test_balance_compaction_preserves_history_and_reopen_ingest(self):
        from economic_machine.pump_research.compact import balances
        with tempfile.TemporaryDirectory() as d:
            s=Store(d,reserve=0)
            with s.db:
                ev=s.evidence('test','x',b'x')
                for i in range(3):
                    tx=dict(signature=str(i),slot=i,index=0,success=True,transfers=[],activity=[],launches=[],balances=[(key(1),2**63+i),(key(2),99-i)])
                    s.ingest(tx,ev,'test','archive_content_verified')
            before=[tuple(r) for r in s.db.execute('SELECT * FROM balances ORDER BY observation,wallet')]
            result=balances(s);self.assertEqual(result['exact_rows_matched'],6)
            self.assertEqual(before,[tuple(r) for r in s.db.execute('SELECT * FROM balances ORDER BY observation,wallet')])
            s.db.close();s=Store(d,reserve=0)
            with s.db:
                tx['signature']='new';tx['slot']=4;s.ingest(tx,ev,'test','archive_content_verified');s.ingest(tx,ev,'test','archive_content_verified')
            self.assertEqual(s.db.execute('SELECT count(*) FROM balances').fetchone()[0],8)
            self.assertEqual(s.trace(key(1))['latest_observed_balance']['lamports'],str(2**63+2))
            s.db.execute('VACUUM')
            self.assertEqual(s.db.execute('SELECT count(*) FROM balances').fetchone()[0],8);s.db.close()

if __name__=='__main__':unittest.main()
