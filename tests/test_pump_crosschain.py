import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research.crosschain import (CrossChain, MAX_RESPONSE, PAGE_SIZE,
    START_MS, key, normalize, parse_vaa, raw_key, sha, iso)
from economic_machine.pump_research.wallet_map import Graph, select_jobs, MODEL
from economic_machine.pump_research.store import canonical, encode


def w(n):
    return encode(bytes([n]) * 32)


STAMP = START_MS + 3600000


def operation(sc=1, dc=23, src=None, dst=None, sequence=7, stamp=STAMP):
    src = src or w(1)
    dst = dst or '0x' + '12' * 20
    token = w(5)
    payload = b'\x01' + (1234).to_bytes(32, 'big') + raw_key(1, token) + b'\0\x01' + raw_key(dc, dst) + dc.to_bytes(2, 'big') + bytes(32)
    body = int(stamp / 1000).to_bytes(4, 'big') + bytes(4) + sc.to_bytes(2, 'big') + bytes([9]) * 32 + sequence.to_bytes(8, 'big') + b'\x20' + payload
    raw = b'\x01' + bytes(4) + b'\x01' + bytes(66) + body
    return dict(id=f'{sc}/{bytes([9]).hex()*32}/{sequence}', emitterChain=sc,
        emitterAddress=dict(hex=bytes([9]).hex()*32), sequence=str(sequence),
        vaa=dict(raw=base64.b64encode(raw).decode(), guardianSetIndex=0, isDuplicated=False),
        content=dict(standarizedProperties=dict(fromChain=sc,toChain=dc,fromAddress=src,toAddress=dst,
            tokenChain=1,tokenAddress=token,amount='1234')),
        sourceChain=dict(chainId=sc, timestamp=iso(stamp),transaction=dict(txHash='A'*64),status='confirmed'),
        targetChain=dict(chainId=dc, timestamp=iso(stamp+1000),transaction=dict(txHash='B'*64),status='completed'))


class Scope:
    def __init__(self):self.reservations=[]
    def guard(self,*a,**k):pass
    def reserve(self,size,purpose):self.reservations.append((size,purpose))


class CrosschainTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        clock=patch('economic_machine.pump_research.crosschain.time.time_ns',return_value=(STAMP+30000)*1000000)
        clock.start();self.addCleanup(clock.stop)
        self.base=Path(self.tmp.name);(self.base/'evidence').mkdir()
        self.g=Graph(self.base);self.addCleanup(self.g.close);self.scope=Scope()
        with self.g.db:
            self.g.db.execute('INSERT INTO roots VALUES(?,?)',(w(99),canonical(dict(mint=w(99)))))
            self.origin=self.g.edge(w(99),w(1),w(99),'early_buy',100,START_MS,signature='fixture',ix=0)
            self.job=self.g.crosschain.enqueue(w(99),w(1),1,w(1),START_MS,origin_edge=self.origin)

    def run_page(self,ops,job=None):
        job=job or self.job
        j=dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?',(job,)).fetchone())
        raw=canonical(dict(operations=ops)).encode()
        with patch.object(self.g.crosschain,'fetch',return_value=(raw,dict(sha256=sha(raw),bytes=len(raw),at_ms=STAMP,url='fixture'))):
            return self.g.crosschain.run(j,self.scope)

    def test_chain_qualified_address_and_unsupported_encoding(self):
        self.assertEqual(key(23,'0x'+'AB'*20),'0x'+'ab'*20)
        for c,a in [(42161,'0x'+'ab'*20),(1,'0x'+'ab'*20),(23,w(1)),(True,w(1))]:
            with self.assertRaises(ValueError):key(c,a)

    def test_existing_research_dates_are_unchanged(self):
        from economic_machine.pump_research import wallet_map,crosschain
        self.assertEqual(wallet_map.START_MS,crosschain.START_MS)
        self.assertEqual(wallet_map.END_MS,crosschain.END_MS)

    def test_message_bytes_bind_fields_but_never_verify_signatures(self):
        r=normalize(operation(),1,w(1),START_MS,STAMP+2000)
        self.assertTrue(r['message_fields_match'])
        for k in ['guardian_signatures_verified','canonical_redemption_verified','same_funds_continuity_proven','custody_gap_resolved']:
            self.assertFalse(r[k])
        self.assertEqual(r['direction'],'outbound')

    def test_mismatched_id_destination_amount_and_token_rejected(self):
        for field,value in [('amount','1235'),('toAddress','0x'+'13'*20),('tokenChain',2),('tokenAddress',w(6))]:
            op=operation();op['content']['standarizedProperties'][field]=value
            with self.assertRaises(ValueError):normalize(op,1,w(1),START_MS,STAMP+2000)
        op=operation();op['id']='wrong'
        with self.assertRaises(ValueError):normalize(op,1,w(1),START_MS,STAMP+2000)

    def test_query_sender_not_relayer_or_unrelated_address(self):
        with self.assertRaisesRegex(ValueError,'QUERY_ADDRESS'):
            normalize(operation(),1,w(9),START_MS,STAMP+2000)
        r=normalize(operation(),23,'0x'+'12'*20,START_MS,STAMP+2000)
        self.assertEqual(r['direction'],'inbound_context_only')

    def test_truncated_vaa_and_unknown_payload_preserved_as_rejected(self):
        with self.assertRaises(ValueError):parse_vaa(base64.b64encode(b'\x01'*10).decode())
        op=operation();raw=bytearray(base64.b64decode(op['vaa']['raw']));raw[6+66+51]=9
        op['vaa']['raw']=base64.b64encode(raw).decode()
        result=self.run_page([op]);self.assertEqual(result['unsupported_or_rejected'],1)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM crosschain_legs').fetchone()[0],0)

    def test_time_window_and_target_chain_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError,'TIME_WINDOW'):
            normalize(operation(),1,w(1),STAMP+1,STAMP+2000)
        op=operation();op['targetChain']['chainId']=30
        with self.assertRaisesRegex(ValueError,'TARGET_CHAIN'):
            normalize(op,1,w(1),START_MS,STAMP+2000)

    def test_forward_route_to_arbitrum_and_back_never_resolves_cex_gap(self):
        self.run_page([operation()])
        child=self.g.db.execute('SELECT job FROM crosschain_jobs WHERE job!=?',(self.job,)).fetchone()[0]
        self.run_page([operation(23,1,'0x'+'12'*20,w(2),sequence=8,stamp=STAMP+2000)],child)
        snapshot=self.g.crosschain.snapshot()
        self.assertEqual(snapshot['summary']['candidate_legs'],2)
        self.assertEqual(snapshot['summary']['independently_verified_routes'],0)
        self.assertEqual(snapshot['summary']['resolved_cex_customer_gaps'],0)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM edges').fetchone()[0],1)
        self.assertTrue(all(not r['same_funds_continuity_proven'] for r in snapshot['legs']))

    def test_unredeemed_route_does_not_spawn_destination_job(self):
        op=operation();op.pop('targetChain');self.run_page([op])
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM crosschain_jobs').fetchone()[0],1)

    def test_failed_redemption_does_not_spawn_destination_job(self):
        op=operation();op['targetChain']['status']='failed';self.run_page([op])
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM crosschain_jobs').fetchone()[0],1)

    def test_legacy_invalid_address_is_preserved_and_skipped(self):
        with self.g.db:self.g.db.execute('INSERT INTO edges VALUES(?,?,?,?,?,?,?,?)',
            ('legacy-bad',w(99),'not-an-address',w(99),'early_buy',1,None,canonical(dict(invalid_address=True))))
        self.g.crosschain.seed()
        self.assertIsNotNone(self.g.db.execute("SELECT 1 FROM edges WHERE id='legacy-bad'").fetchone())

    def test_dead_origin_revokes_descendants(self):
        self.run_page([operation()]);child=self.g.db.execute('SELECT job FROM crosschain_jobs WHERE job!=?',(self.job,)).fetchone()[0]
        with self.g.db:self.g.db.execute("UPDATE edges SET body=json_set(body,'$.dead_branch',1) WHERE id=?",(self.origin,))
        self.assertFalse(self.g.crosschain.valid_job(child))
        self.assertEqual(self.run_page([],child)['state'],'crosschain_anchor_revoked')

    def test_reorg_body_conflict_revokes_prior_branch(self):
        self.run_page([operation()]);child=self.g.db.execute('SELECT job FROM crosschain_jobs WHERE job!=?',(self.job,)).fetchone()[0]
        # Same emitter/sequence, different body, retained from another page.
        op=operation(stamp=STAMP+10000)
        with self.g.db:another=self.g.crosschain.enqueue(w(99),w(1),1,w(1),START_MS+1,origin_edge=self.origin)
        self.run_page([op],another)
        self.assertFalse(self.g.crosschain.valid_job(child))
        self.assertTrue(any(r['conflicting_message'] for r in self.g.crosschain.snapshot()['legs']))

    def test_empty_response_means_snapshot_exhaustion_not_no_transfers(self):
        self.run_page([])
        state=self.g.db.execute('SELECT state FROM frontier WHERE id=?',(self.job,)).fetchone()[0]
        self.assertEqual(state,'bridge_snapshot_exhausted')
        self.assertFalse(self.g.crosschain.snapshot()['scope']['complete_history'])

    def test_http_error_preserves_cursor_and_no_page_success(self):
        import urllib.error
        before=self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(self.job,)).fetchone()[0]
        j=dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?',(self.job,)).fetchone())
        with patch.object(self.g.crosschain,'fetch',side_effect=urllib.error.HTTPError('fixture',429,'rate limit',{},None)):
            with self.assertRaises(urllib.error.HTTPError):self.g.crosschain.run(j,self.scope)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(self.job,)).fetchone()[0],before)
        self.assertEqual(self.g.crosschain.snapshot()['summary']['pages'],0)

    def test_repeated_nonempty_page_does_not_advance_cursor(self):
        ops=[operation(sequence=i) for i in range(PAGE_SIZE)];self.run_page(ops)
        with self.assertRaisesRegex(ValueError,'REPEATED_PAGE'):self.run_page(ops)
        self.assertEqual(json.loads(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(self.job,)).fetchone()[0])['page'],1)

    def test_seven_day_windows_continue_through_empty_pages(self):
        from economic_machine.pump_research.crosschain import WINDOW_MS
        meta=json.loads(self.g.db.execute('SELECT body FROM crosschain_jobs WHERE job=?',(self.job,)).fetchone()[0])
        meta['snapshot_end_ms']=START_MS+WINDOW_MS*2+1000
        with self.g.db:self.g.db.execute('UPDATE crosschain_jobs SET body=? WHERE job=?',(canonical(meta),self.job))
        self.run_page([])
        first=json.loads(self.g.db.execute('SELECT body FROM crosschain_pages WHERE job=? AND page=0',(self.job,)).fetchone()[0])
        self.assertLessEqual(first['window_end_ms']-first['window_start_ms'],WINDOW_MS)
        self.assertEqual(self.g.db.execute('SELECT state FROM frontier WHERE id=?',(self.job,)).fetchone()[0],'pending')
        self.run_page([]);self.run_page([])
        pages=[json.loads(r[0]) for r in self.g.db.execute('SELECT body FROM crosschain_pages ORDER BY page')]
        self.assertEqual(pages[1]['window_end_ms'],pages[0]['window_start_ms']-1)
        self.assertEqual(pages[-1]['window_start_ms'],START_MS)
        self.assertEqual(self.g.db.execute('SELECT state FROM frontier WHERE id=?',(self.job,)).fetchone()[0],'bridge_snapshot_exhausted')

    def test_restart_and_seed_preserve_existing_frontier(self):
        self.run_page([]);before=[tuple(r) for r in self.g.db.execute('SELECT * FROM frontier ORDER BY id')]
        self.g.crosschain=CrossChain(self.g);self.g.crosschain.seed()
        self.assertEqual(before,[tuple(r) for r in self.g.db.execute('SELECT * FROM frontier ORDER BY id')])

    def test_custody_context_survives_bridge_recursion(self):
        with self.g.db:
            edge=self.g.edge(w(99),w(1),w(2),'funding_record',101,START_MS,destination_labeled=True)
        self.g.crosschain.seed()
        job=self.g.db.execute('''SELECT c.job FROM crosschain_jobs c JOIN custody_jobs j ON j.job=c.job LIMIT 1''').fetchone()[0]
        self.g.custody.enter(job)
        try:self.run_page([operation(src=w(2))],job)
        finally:self.g.custody.leave()
        children=self.g.db.execute("SELECT j.job FROM crosschain_jobs c JOIN custody_jobs j ON j.job=c.job WHERE json_extract(c.body,'$.chain')=23").fetchall()
        self.assertEqual(len(children),1)

    def test_model_ignored_bridge_gets_reserved_slot_without_rewriting_choice(self):
        other=self.g.enqueue(w(99),w(3),100,START_MS,0,'local_forward');self.g.db.commit()
        class Budget:
            def reserve(self,req,now):
                self.req=req;return 1
            def finish(self,*args):pass
        b=Budget()
        response=canonical(dict(model=MODEL,choices=[dict(message=dict(content=canonical(dict(job_ids=[other],reason='fixture',remaining_gaps=[]))))])).encode()
        with patch('economic_machine.pump_research.wallet_map.provider_call',return_value=response):
            jobs=select_jobs(self.g,b,'unused',self.scope)
        self.assertIn(self.job,[j['id'] for j in jobs])
        row=json.loads(self.g.db.execute('SELECT body FROM investigations').fetchone()[0])
        self.assertEqual(row['job_ids'],[other]);self.assertEqual(row['engine_reserved_crosschain_job'],self.job)
        self.assertLess(len(canonical(b.req).encode()),16000)


if __name__=='__main__':unittest.main()
