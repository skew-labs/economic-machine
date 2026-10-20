import hashlib
import json
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import cbor2
from economic_machine.pump_research.store import Store,encode,decode,address,ath_quality,canonical,PUMP
from economic_machine.pump_research.wire import extract,CREATES,raw_transaction,varint
from economic_machine.pump_research.archive import nodes,dataframe,checksum,link,Archive
from economic_machine.pump_research.planner import validate,ResearchBudget
from economic_machine.pump_research.ticking import payload,HEADER

def key(i):return bytes([i])*32
def meta(failed=False):return NS(loaded_writable_addresses=[],loaded_readonly_addresses=[],post_balances=[],pre_token_balances=[],post_token_balances=[],inner_instructions=[],HasField=lambda k:failed if k=='err' else False)
def vint(n):
    out=b''
    while n>=128:out+=bytes([(n&127)|128]);n>>=7
    return out+bytes([n])
def car(value):
    body=cbor2.dumps(value);cid=b'\x01\x71\x12\x20'+hashlib.sha256(body).digest();data=cid+body
    return vint(len(data))+data,cid

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name,reserve=0)
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def ev(self):return self.s.evidence('test','fixture',b'test')
    def tx(self,src=1,dst=2,slot=10):
        return extract(encode(b'x'*64),[key(src),key(dst),b'\0'*32],[(2,[0,1],struct.pack('<IQ',2,100))],meta(),slot)
    def test_base58_roundtrip(self):
        for b in [b'\0'*32,key(1),key(255)]:self.assertEqual(decode(encode(b)),b)
    def test_address_rejects_bad_width(self):self.assertFalse(address('1'*33));self.assertFalse(address('0'*44))
    def test_ath_extreme_not_ranked_as_truth(self):self.assertEqual(ath_quality('1.9e23'),'quarantined_extreme_value')
    def test_ath_plausible_still_unverified(self):self.assertEqual(ath_quality('1000000'),'provider_claim_unverified')
    def test_ath_nonfinite(self):
        for v in ['nan','inf','-1','not-number']:self.assertEqual(ath_quality(v),'invalid')
    def test_search_quotes_not_sql(self):
        with self.s.db:self.s.candidates([dict(mint=encode(key(1)),creator=encode(key(2)),name='Alpha Robot',symbol='ABC',ath_market_cap=12)],self.ev(),0)
        self.assertEqual(len(self.s.search('Alpha')),1);self.assertEqual(self.s.search('" OR 1=1 --'),[])
    def test_failed_transactions_have_no_transfers(self):
        tx=extract(encode(b'x'*64),[key(1),key(2),b'\0'*32],[(2,[0,1],struct.pack('<IQ',2,100))],meta(True),10)
        self.assertEqual(tx['transfers'],[])
    def test_balance_delta_not_transfer(self):
        m=meta();m.post_balances=[10,20]
        tx=extract(encode(b'x'*64),[key(1),key(2)],[],m,10)
        self.assertEqual(tx['transfers'],[]);self.assertEqual(len(tx['balances']),2)
    def test_raw_integer_precision(self):
        tx=extract(encode(b'x'*64),[key(1),key(2),b'\0'*32],[(2,[0,1],struct.pack('<IQ',2,2**63+9))],meta(),10)
        self.assertEqual(tx['transfers'][0]['amount'],str(2**63+9))
    def test_invalid_account_index(self):
        with self.assertRaises(ValueError):extract(encode(b'x'*64),[key(1)],[(2,[],b'')],meta(),10)
    def test_payer_creator_distinct(self):
        data=next(iter(CREATES))+b''.join(struct.pack('<I',len(x))+x for x in [b'Name',b'SYM',b'https://untrusted'])+key(10)
        tx=extract(encode(b'x'*64),[key(i) for i in range(1,9)]+[decode(PUMP)],[(8,list(range(8)),data)],meta(),10)
        launch=tx['launches'][0];self.assertEqual(launch['payer'],encode(key(8)));self.assertEqual(launch['creator'],encode(key(10)))
    def test_replay_deduplicates_same_provenance(self):
        with self.s.db:
            e=self.ev();self.s.ingest(self.tx(),e,'test','archive_content_verified');self.s.ingest(self.tx(),e,'test','archive_content_verified')
        self.assertEqual(self.s.summary()['counts']['transfers'],1)
    def test_causal_trace_excludes_later_funding(self):
        with self.s.db:
            e=self.ev()
            for tx in [self.tx(1,2,10),self.tx(3,1,11),self.tx(4,1,9)]:self.s.ingest(tx,e,'test','archive_content_verified')
        out=self.s.trace(encode(key(2)),before=10)
        self.assertEqual({x['source'] for x in out['edges']},{encode(key(1)),encode(key(4))})
        self.assertFalse(out['current_balance_verified']);self.assertFalse(out['ownership_inferred'])
    def test_graph_bounds(self):
        with self.assertRaises(ValueError):self.s.trace(encode(key(1)),depth=99)
    def test_dead_branch_excluded(self):
        with self.s.db:
            self.s.ingest(self.tx(),self.ev(),'test','processed_unconfirmed')
            self.s.db.execute("UPDATE observations SET finality='dead_branch'")
        self.assertEqual(self.s.trace(encode(key(2)))['edges'],[])
    def test_same_slot_future_transfer_excluded(self):
        with self.s.db:
            tx=self.tx();tx['index']=10;self.s.ingest(tx,self.ev(),'test','archive_content_verified')
        self.assertEqual(self.s.trace(encode(key(2)),before=10,before_order=(9,0))['edges'],[])
    def test_current_v3_pump_accounts(self):
        tx=extract(encode(b'x'*64),[key(i) for i in range(1,10)]+[decode(PUMP)],[(9,list(range(9)),bytes([7,5,29,196,245,23,101,80])+b'\0'*16)],meta(),10)
        self.assertEqual(tx['activity'][0]['wallet'],encode(key(9)))
        self.assertEqual(tx['activity'][0]['mint'],encode(key(2)))
    def test_watch_follows_funding_not_ownership(self):
        with self.s.db:
            ev=self.ev();self.s.db.execute('INSERT INTO watchlist VALUES(?,?,?,?,?,?)',(encode(key(1)),encode(key(1)),0,'test',0,ev))
            watches=self.s.watch_roots();tx=self.tx();tx['transfers'][0]['amount']='10000000'
            self.s.ingest(tx,ev,'test','processed_unconfirmed')
            self.assertEqual(self.s.expand_watch(tx,ev,watches),[encode(key(2))])
            self.assertEqual(watches[encode(key(2))]['reason'],'outgoing_funding_association')
    def test_dust_does_not_expand_watch(self):
        with self.s.db:
            ev=self.ev();self.s.db.execute('INSERT INTO watchlist VALUES(?,?,?,?,?,?)',(encode(key(1)),encode(key(1)),0,'test',0,ev))
            self.assertEqual(self.s.expand_watch(self.tx(),ev,self.s.watch_roots()),[])
    def test_followup_is_bounded_and_idempotent(self):
        from economic_machine.pump_research.cli import enqueue_funding_followups
        mint=encode(key(4))
        with self.s.db:
            tx=self.tx(slot=300000000);tx['launches']=[dict(ix=5,mint=mint,payer=encode(key(1)),creator='',name='test',symbol='T')]
            self.s.ingest(tx,self.ev(),'test','archive_content_verified')
            self.s.db.execute('INSERT INTO jobs(id,kind,args,status) VALUES(?,?,?,?)',('launch:'+mint,'launch_window',canonical(dict(mint=mint)),'complete'))
        self.assertEqual(len(enqueue_funding_followups(self.s)),1);self.assertEqual(enqueue_funding_followups(self.s),[])
        args=json.loads(self.s.db.execute("SELECT args FROM jobs WHERE kind='slot_window'").fetchone()[0]);self.assertEqual(args['end']-args['start']+1,128)
    def test_content_object_tamper(self):
        with self.s.db:e=self.ev()
        h=hashlib.sha256(b'test').hexdigest();(self.s.root/'objects'/h).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.ev()
    def test_car_cid_integrity(self):
        raw,cid=car([2,10,[],[],[9,100],None]);self.assertEqual(nodes(raw)[cid][1],10)
        with self.assertRaises(ValueError):nodes(raw[:-1]+bytes([raw[-1]^1]))
    def test_car_truncation(self):
        raw,_=car([2,10,[],[],[9,100],None])
        with self.assertRaises(ValueError):nodes(raw[:-3])
    def test_frame_checksum(self):
        b=b'frame';self.assertEqual(dataframe([6,checksum(b),0,1,b],{}),b)
        with self.assertRaises(ValueError):dataframe([6,checksum(b)+1,0,1,b],{})
    def test_frame_missing_link(self):
        with self.assertRaises(ValueError):dataframe([6,None,0,2,b'a',[cbor2.CBORTag(42,b'\0missing')]],{})
    def test_frame_gap(self):
        with self.assertRaises(ValueError):dataframe([6,None,2,1,b'a'],{})
    def test_varint_overflow(self):
        with self.assertRaises(ValueError):varint(b'\xff'*10)
    def test_wire_signature_verification(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
        signing=Ed25519PrivateKey.generate();pub=signing.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        message=bytes([1,0,0,1])+pub+b'\0'*32+b'\0';tx=b'\1'+signing.sign(message)+message
        self.assertEqual(raw_transaction(tx)[1],[pub])
        with self.assertRaises(ValueError):raw_transaction(tx[:4]+bytes([tx[4]^1])+tx[5:])
    def test_crc_corruption(self):
        import zlib
        self.assertEqual(payload(b'SLI1',b'abc',zlib.crc32(b'abc')),b'abc')
        with self.assertRaises(ValueError):payload(b'SLI1',b'abc',0)
    def test_bounded_decompression(self):
        import zlib
        b=zlib.compress(b'x'*100)
        self.assertEqual(payload(b'SLZ1',b,zlib.crc32(b'x'*100)),b'x'*100)
        with self.assertRaises(ValueError):payload(b'SLZ1',b+b'junk',zlib.crc32(b'x'*100))
    def test_model_cannot_inject_command(self):
        with self.assertRaises(ValueError):validate({'candidate_ids':['x'],'hypotheses':['h'],'limitations':['l'],'command':'buy'}, {'x':{}})
    def test_model_cannot_choose_arbitrary_wallet(self):
        with self.assertRaises(ValueError):validate({'candidate_ids':['y'],'hypotheses':['h'],'limitations':['l']}, {'x':{}})
    def test_model_plan_valid(self):
        v={'candidate_ids':['x'],'hypotheses':['h'],'limitations':['l']};self.assertEqual(validate(v,{'x':{}}),v)
    def test_no_rank_or_profit_claim_from_counts(self):
        s=self.s.summary();self.assertFalse(s['verified_all_time_top1000']);self.assertFalse(s['trading_enabled'])
    def test_unknown_call_cost_remains_reserved(self):
        from economic_machine.muse_maker import MODEL
        b=ResearchBudget(Path(self.tmp.name)/'budget.sqlite3','test',int(time.time()*1000)+10000,'0.03',1)
        req=dict(model=MODEL,max_tokens=4096);b.reserve(req,int(time.time()*1000))
        self.assertEqual(b.totals()['unconfirmed_cost_reservations'],1)
        with self.assertRaises(ValueError):b.reserve(dict(req,extra=1),int(time.time()*1000))
        b.db.close()
    def test_no_full_car_on_ignored_range(self):
        from economic_machine.pump_research.archive import get
        response=NS(status=200,headers={},read=lambda _:b'large')
        class CM:
            def __enter__(self):return response
            def __exit__(self,*a):pass
        with patch('urllib.request.build_opener',return_value=NS(open=lambda *a,**k:CM())):
            with self.assertRaises(ValueError):get('https://files.old-faithful.net/750/epoch-750.car',12,(0,11))

if __name__=='__main__':unittest.main()
