import base64
import ctypes
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cbor2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from economic_machine.pump_research.cli import proto
from economic_machine.pump_research.solana_export import (
    Facts, block_records, read_pack, search, transaction_facts, sha, un64)
from economic_machine.pump_research.store import encode, canonical

try:
    _, META = proto('/srv/economic-machine/collector')
except ModuleNotFoundError as exc:
    raise unittest.SkipTest('Collector-generated Yellowstone protobuf bindings are required for export integration tests') from exc
PB = importlib.import_module(META.__module__)
SLOT = 436099800


def compress(raw):
    lib=ctypes.CDLL('libzstd.so.1')
    lib.ZSTD_compressBound.argtypes=[ctypes.c_size_t];lib.ZSTD_compressBound.restype=ctypes.c_size_t
    lib.ZSTD_compress.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
    lib.ZSTD_compress.restype=ctypes.c_size_t
    out=ctypes.create_string_buffer(lib.ZSTD_compressBound(len(raw)))
    size=lib.ZSTD_compress(out,len(out),raw,len(raw),1)
    if size>len(out):raise ValueError('FIXTURE_COMPRESSION')
    return out.raw[:size]


def vi(n):
    raw = bytearray()
    while n >= 128: raw.append((n & 127)|128); n >>= 7
    raw.append(n); return bytes(raw)


def fixture(failed=False, missing=False, no_reward=False, unknown=False, cpi=False):
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    keys = [key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw), bytes([2])*32, bytes(32)]
    data = (2).to_bytes(4,'little')+(7).to_bytes(8,'little')
    message = bytes([1,0,1,3])+b''.join(keys)+bytes(32)+bytes([1,2,2,0,1,len(data)])+data
    wire = b'\1'+key.sign(message)+message
    meta = META(fee=5000,pre_balances=[10000,0,1],post_balances=[4993,7,1])
    if failed: meta.err.err=b'failure';meta.post_balances[:]=[5000,0,1]
    if missing: del meta.pre_balances[:];meta.inner_instructions_none=True;meta.log_messages_none=True
    for phase,owner,amount in [('pre',9,'18446744073709551615'),('post',10,'18446744073709551614')]:
        item=getattr(meta,phase+'_token_balances').add(account_index=1,mint=encode(bytes([3])*32),owner=encode(bytes([owner])*32))
        item.ui_token_amount.amount=amount;item.ui_token_amount.decimals=6
    if cpi:
        child=meta.inner_instructions.add(index=0).instructions.add(program_id_index=2,accounts=bytes([0,1]),data=data)
        child.stack_height=2
        program=encode(bytes(32));meta.log_messages.extend([f'Program {program} invoke [1]',f'Program {program} invoke [2]',f'Program {program} success',f'Program {program} success'])
    metadata=meta.SerializeToString()+(vi(99*8)+vi(17) if unknown else b'')
    raw=[]
    def node(n):
        body=cbor2.dumps(n);cid=bytes.fromhex('01711220')+hashlib.sha256(body).digest();raw.append(vi(len(cid)+len(body))+cid+body);return cbor2.CBORTag(42,b'\0'+cid)
    frame=lambda data:[6,None,None,None,data]
    tx=node([0,frame(wire),frame(compress(metadata)),SLOT,0])
    entry=node([1,0,bytes(32),[tx]])
    rewards=PB.Rewards();rewards.rewards.add(pubkey=encode(bytes([11])*32),lamports=7,post_balance=17,reward_type=1)
    reward=None if no_reward else node([5,SLOT,frame(compress(rewards.SerializeToString()))])
    node([2,SLOT,[],[entry],[SLOT-1,1785392560,414157887],reward])
    return dict(raw=b''.join(raw),wire=wire,metadata=metadata,keys=[encode(x) for x in keys],meta=meta)


class FieldTests(unittest.TestCase):
    def test_all_wire_metadata_and_unknown_fields_retained(self):
        f=fixture(unknown=True);rows=list(block_records(f['raw'],SLOT,META,PB.Rewards));t=rows[1]
        self.assertEqual(un64(t['wire_b64']),f['wire']);self.assertEqual(un64(t['metadata_protobuf_b64']),f['metadata'])
        self.assertEqual(t['fee_lamports'],'5000');self.assertEqual(t['token_balances'][0]['amount'],'18446744073709551615')
        self.assertEqual(t['native_balances'][0]['pre'],'10000');self.assertEqual(t['native_balances'][0]['post'],'4993')
        self.assertEqual(rows[-2]['rewards']['rewards'][0]['lamports'],'7');self.assertFalse(rows[0]['vote_filter_applied'])
    def test_failed_transaction_retains_fee_and_both_balance_sides(self):
        f=fixture(failed=True);t=list(block_records(f['raw'],SLOT,META,PB.Rewards))[1]
        self.assertFalse(t['success']);self.assertEqual(t['native_balances'][0]['post'],'5000')
        self.assertEqual(t['instructions'][0]['effect_status'],'transaction_failed');self.assertTrue(t['metadata']['err'])
    def test_inner_cpi_is_retained_and_commitment_separate(self):
        f=fixture(cpi=True);t=list(block_records(f['raw'],SLOT,META,PB.Rewards))[1]
        self.assertEqual(len(t['instructions']),2);self.assertEqual(t['instructions'][1]['effect_status'],'committed_from_runtime_logs')
        f['meta'].ClearField('log_messages')
        t=transaction_facts(f['wire'],f['meta'].SerializeToString(),META,SLOT,0,b'x')
        self.assertEqual(len(t['instructions']),2);self.assertEqual(t['instructions'][1]['effect_status'],'unresolved_or_reverted')
        self.assertTrue(t['instruction_commit_gaps'])
    def test_missing_balances_logs_and_rewards_are_not_zero_evidence(self):
        f=fixture(missing=True,no_reward=True);rows=list(block_records(f['raw'],SLOT,META,PB.Rewards))
        self.assertIsNone(rows[1]['native_balances'][0]['pre'])
        self.assertIn('native_pre_balance_cardinality',rows[-1]['missing_fields']);self.assertIn('block_rewards_unavailable',rows[-1]['missing_fields'])
        self.assertFalse(rows[-1]['source_fields_without_gaps']);self.assertTrue(rows[-1]['source_record_export_complete'])
    def test_bad_cid_or_signature_rejected(self):
        f=fixture();raw=bytearray(f['raw']);raw[-2]^=1
        with self.assertRaises(ValueError):list(block_records(bytes(raw),SLOT,META,PB.Rewards))
        wire=bytearray(f['wire']);wire[4]^=1
        with self.assertRaises(ValueError):transaction_facts(bytes(wire),f['metadata'],META,SLOT,0,b'x')
    def test_wrong_slot_rejected(self):
        with self.assertRaises(ValueError):list(block_records(fixture()['raw'],SLOT+1,META,PB.Rewards))


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=Facts(self.root,{'test':1});self.addCleanup(self.store.close);self.f=fixture()
    def go(self,**kw):return self.store.export(self.f['raw'],SLOT,META,PB.Rewards,**kw)
    def test_complete_search_and_owner_boundary_matches(self):
        result=self.go();self.assertEqual(result['transactions'],1)
        for who in [self.f['keys'][0],encode(bytes([9])*32),encode(bytes([10])*32)]:
            found=search(self.root,who);self.assertEqual(len(found['hits']),1);self.assertFalse(found['same_owner_proven'])
        self.assertEqual(search(self.root,encode(bytes([12])*32))['hits'],[])
    def test_repeated_export_is_idempotent(self):
        a=self.go();b=self.go();self.assertTrue(b['resumed']);self.assertEqual(a['artifact_hash'],b['artifact_hash'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM transactions').fetchone()[0],1)
    def test_reward_only_address_is_searchable(self):
        self.go();hits=search(self.root,encode(bytes([11])*32))['hits']
        self.assertEqual(len(hits),1);self.assertEqual(hits[0]['kind'],'reward')
        self.assertEqual(hits[0]['reward']['lamports'],'7');self.assertEqual(hits[0]['slot'],SLOT)
    def test_crash_mid_stream_preserves_partial_no_index_and_restarts(self):
        def fault(stage,n):
            if stage=='record' and n==1:raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):self.go(fault=fault)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM transactions').fetchone()[0],0)
        self.assertEqual(len(list(self.root.glob('*.partial-*'))),1)
        self.go();self.assertEqual(self.store.db.execute('SELECT count(*) FROM failures').fetchone()[0],1)
    def test_crash_after_verified_artifact_reuses_same_bytes(self):
        def fault(stage,n):
            if stage=='verified':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):self.go(fault=fault)
        path=next(self.root.glob('*.jsonl.gz'));before=sha(path.read_bytes());self.go()
        self.assertEqual(sha(path.read_bytes()),before)
    def test_crash_artifact_changed_derived_facts_rejected(self):
        def fault(stage,n):
            if stage=='verified':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):self.go(fault=fault)
        path=next(self.root.glob('*.jsonl.gz'))
        records=[json.loads(x) for x in gzip.decompress(path.read_bytes()).splitlines()]
        records[1]['fee_lamports']='0'
        path.write_bytes(gzip.compress(b''.join(canonical(x).encode()+b'\n' for x in records)))
        with self.assertRaisesRegex(ValueError,'EXPORT_SOURCE_FACT_MISMATCH'):self.go()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM transactions').fetchone()[0],0)
    def test_crash_mid_index_rolls_back_and_recovers(self):
        def fault(stage,n):
            if stage=='index':raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):self.go(fault=fault)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM appearances').fetchone()[0],0)
        self.go();self.assertEqual(len(search(self.root,self.f['keys'][0])['hits']),1)
    def test_cloud_failure_retains_original_and_does_not_index(self):
        def archive(path,digest):raise RuntimeError('cloud failed')
        before=sha(self.f['raw'])
        with self.assertRaisesRegex(RuntimeError,'cloud failed'):self.go(archive=archive)
        self.assertEqual(sha(self.f['raw']),before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM blocks').fetchone()[0],0)
        self.assertEqual(len(list(self.root.glob('*.jsonl.gz'))),1)
    def test_cloud_mismatched_receipt_does_not_index(self):
        with self.assertRaisesRegex(ValueError,'EXPORT_CLOUD_RECEIPT'):
            self.go(archive=lambda p,h:dict(sha256='0'*64,bytes=p.stat().st_size,full_download_verified=True))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM transactions').fetchone()[0],0)
    def test_conflicting_source_never_overwrites_commit(self):
        self.go()
        with self.assertRaisesRegex(ValueError,'EXPORT_SLOT_CONFLICT'):
            self.store.export(fixture(failed=True)['raw'],SLOT,META,PB.Rewards)
    def test_committed_artifact_tampering_rejected(self):
        self.go();p=next(self.root.glob('*.jsonl.gz'));p.write_bytes(p.read_bytes()+b'x')
        with self.assertRaisesRegex(ValueError,'EXPORT_COMMITTED_ARTIFACT_CHANGED'):self.go()
    def test_source_guards_apply_during_decode_and_replay(self):
        calls=[]
        def guard():
            calls.append(1)
            if len(calls)>3:raise ValueError('EXPORT_DEADLINE')
        self.store.guard=guard
        with self.assertRaisesRegex(ValueError,'EXPORT_DEADLINE'):self.go()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM blocks').fetchone()[0],0)
    def test_pack_missing_end_and_decompression_bound(self):
        self.go();p=next(self.root.glob('*.jsonl.gz'));lines=gzip.decompress(p.read_bytes()).splitlines(keepends=True)
        bad=self.root/'bad.gz';bad.write_bytes(gzip.compress(b''.join(lines[:-1])))
        with self.assertRaisesRegex(ValueError,'EXPORT_INCOMPLETE_PACK'):list(read_pack(bad))
        with patch('economic_machine.pump_research.solana_export.MAX_EXPANDED',32):
            with self.assertRaisesRegex(ValueError,'EXPORT_EXPANSION_BOUND'):list(read_pack(p))
    def test_changed_policy_and_invalid_search_rejected(self):
        with self.assertRaisesRegex(ValueError,'EXPORT_WRITER_BUSY'):Facts(self.root,{'test':2})
        self.store.close()
        with self.assertRaisesRegex(ValueError,'EXPORT_POLICY_CHANGED'):Facts(self.root,{'test':2})
        with self.assertRaises(ValueError):search(self.root,"' OR 1=1")


if __name__=='__main__':unittest.main()
