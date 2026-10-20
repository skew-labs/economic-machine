import copy
import ctypes
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cbor2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from economic_machine.pump_research.address_index import AddressIndex, parse_index_block, read_indexed_slot, search
from economic_machine.pump_research.gsfa_history import GsfaHistory, corroborate_candidate
from economic_machine.pump_research.raw_history import ArchiveDeferred
from economic_machine.pump_research.store import encode


EPOCH = 685
SLOT = EPOCH * 432000 + 120
CID_PREFIX = b'\x01\x71\x12\x20'


def vint(n):
    out = []
    while n >= 128:
        out.append((n & 127) | 128)
        n >>= 7
    return bytes(out + [n])


def compress(raw):
    lib = ctypes.CDLL('libzstd.so.1')
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.ZSTD_compress.restype = ctypes.c_size_t
    output = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(raw)))
    size = lib.ZSTD_compress(output, len(output), raw, len(raw), 1)
    if size > len(output):
        raise ValueError('fixture compression')
    return output.raw[:size]


def section(value):
    raw = cbor2.dumps(value)
    cid = CID_PREFIX + hashlib.sha256(raw).digest()
    return cid, vint(len(cid) + len(raw)) + cid + raw


def tag(cid):
    return cbor2.CBORTag(42, b'\0' + cid)


class Metadata:
    @staticmethod
    def FromString(raw):
        body = json.loads(raw)
        return SimpleNamespace(
            HasField=lambda name: name == 'err' and body['failed'],
            loaded_writable_addresses=[bytes.fromhex(x) for x in body['loaded']],
            loaded_readonly_addresses=[], pre_token_balances=[], post_token_balances=[],
            inner_instructions=[], post_balances=[], log_messages=[])


def make_block(*, failed=False, slot=SLOT, tamper=False, loaded=False, orphan=False):
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    payer = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    destination = bytes([222]) * 32
    keys = [payer, destination, bytes(32)] if not loaded else [payer, bytes(32)]
    # Valid signed system transfer, with optional v0 loaded destination account.
    data = struct.pack('<IQ', 2, 123456789)
    message = bytes([1, 0, 1]) + vint(len(keys)) + b''.join(keys) + bytes(32)
    program = 1 if loaded else 2
    recipient = 2 if loaded else 1
    message += b'\x01' + bytes([program, 2, 0, recipient]) + vint(len(data)) + data
    if loaded:
        message = b'\x80' + message + b'\x01' + bytes([33]) * 32 + b'\x01\x00\x00'
    signature = private.sign(message)
    wire = b'\x01' + signature + message
    if tamper:
        wire = wire[:-1] + bytes([wire[-1] ^ 1])
    metadata = compress(json.dumps(dict(failed=failed, loaded=[destination.hex()] if loaded else [])).encode())
    tx = [0, [6, None, 0, 1, wire], [6, None, 0, 1, metadata], slot, 7]
    tx_cid, tx_raw = section(tx)
    entry_cid, entry_raw = section([1, None, None, [] if orphan else [tag(tx_cid)]])
    _, block_raw = section([2, slot, None, [tag(entry_cid)], [None, 1729000000]])
    raw = tx_raw + entry_raw + block_raw
    candidate = dict(slot=slot, car_offset=1000, car_size=len(tx_raw),
                     index_has_metadata=True, index_success=not failed, index_is_vote=False)
    return dict(raw=raw, candidate=candidate, payer=encode(payer), destination=encode(destination),
                signature=encode(signature), tx_size=len(tx_raw))


class MemoryArchive:
    """Use the real block reconstruction/indexing path with in-memory ranges."""
    def __init__(self, fixtures, index):
        self.fixtures = {f['candidate']['slot']: f for f in fixtures}
        self.index = index
        self.meta_type = Metadata
        self.calls = []
        self.error = None
        self.absent = False

    def request(self, url, start, end):
        if url.endswith('-slot-ranges.raw'):
            slot = EPOCH * 432000 + start // 12
            self.current = self.fixtures.get(slot)
            return struct.pack('<QI', 1000, len(self.current['raw']) if self.current and not self.absent else 0)
        return self.current['raw'][start - 1000:end - 1000 + 1]

    def read_slot(self, slot):
        self.calls.append(slot)
        if self.error:
            raise self.error
        return read_indexed_slot(self, slot, self.index, lambda: None)


class FakeGsfa:
    def __init__(self, candidates, wallet, epoch=EPOCH):
        self.epoch = epoch
        self.identity = 'index-identity'
        self.root_cid = CID_PREFIX.hex() + '00' * 32
        self.budget = SimpleNamespace(check=lambda: None)
        self.candidates = candidates
        self.wallet = wallet
        self.calls = []

    def page(self, wallet, limit=32, cursor=None):
        self.calls.append(copy.deepcopy(cursor))
        start = 0 if cursor is None else cursor['n']
        rows = self.candidates[start:start + limit]
        end = start + len(rows)
        nxt = dict(n=end) if end < len(self.candidates) else None
        return dict(wallet=wallet, epoch=self.epoch, index_identity=self.identity,
                    candidates=rows, next_cursor=nxt, index_chain_exhausted=nxt is None)


class CorroborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = AddressIndex(Path(self.tmp.name) / 'address.sqlite3')
        self.addCleanup(self.index.close)

    def proof(self, fixture=None, wallet=None, candidate=None):
        fixture = fixture or make_block()
        archive = MemoryArchive([fixture], self.index)
        block, receipt = archive.read_slot(fixture['candidate']['slot'])
        return corroborate_candidate(wallet or fixture['payer'], candidate or fixture['candidate'], block, receipt, Metadata)

    def test_signed_outgoing_and_incoming_and_searchable_account_keys(self):
        f = make_block()
        outgoing = self.proof(f)
        incoming = self.proof(f, f['destination'])
        self.assertEqual(outgoing['state'], 'verified_success')
        self.assertEqual(outgoing['outgoing'][0]['amount'], '123456789')
        self.assertEqual(incoming['incoming'][0]['source'], f['payer'])
        self.assertEqual(incoming['signature'], f['signature'])
        self.assertFalse(incoming['independent_consensus_verified'])
        self.assertFalse(incoming['same_funds_continuity_proven'])
        self.assertFalse(incoming['complete_address_history'])
        self.assertEqual(len(search(self.index.path, f['destination'])['transactions']), 1)

    def test_loaded_address_is_checked_using_execution_metadata(self):
        f = make_block(loaded=True)
        out = self.proof(f, f['destination'])
        self.assertEqual(out['state'], 'verified_success')
        self.assertEqual(len(out['incoming']), 1)
        self.assertFalse(out['loaded_account_resolution_verified_by_consensus'])

    def test_failed_transaction_retained_without_fund_movement(self):
        out = self.proof(make_block(failed=True))
        self.assertEqual(out['state'], 'verified_failed_transaction')
        self.assertEqual(out['outgoing'], [])
        self.assertTrue(out['signature_verified'])

    def test_compact_fingerprint_false_positive_not_wallet_membership(self):
        out = self.proof(wallet=encode(bytes([66]) * 32))
        self.assertEqual(out['state'], 'rejected_address_mismatch')
        self.assertFalse(out['address_membership_verified'])

    def test_success_flag_mismatch_is_rejected(self):
        f = make_block()
        out = self.proof(f, candidate=dict(f['candidate'], index_success=False))
        self.assertEqual(out['state'], 'rejected_index_metadata_flags_mismatch')

    def test_missing_metadata_flag_not_silently_promoted(self):
        f = make_block()
        out = self.proof(f, candidate=dict(f['candidate'], index_has_metadata=False))
        self.assertEqual(out['state'], 'rejected_index_metadata_flags_mismatch')

    def test_valid_section_must_be_linked_by_block(self):
        self.assertEqual(self.proof(make_block(orphan=True))['state'], 'rejected_candidate_not_linked_once')

    def test_invalid_wire_signature_is_rejected_even_with_recomputed_cid(self):
        out = self.proof(make_block(tamper=True))
        self.assertEqual(out['state'], 'rejected_wire_transaction')
        self.assertEqual(out['reason'], 'TRANSACTION_SIGNATURE_INVALID')

    def test_offset_and_size_must_match_actual_section(self):
        f = make_block()
        self.assertEqual(self.proof(f, candidate=dict(f['candidate'], car_offset=999))['state'], 'rejected_candidate_outside_block')
        self.assertEqual(self.proof(f, candidate=dict(f['candidate'], car_offset=1001))['state'], 'rejected_candidate_not_section_boundary')
        self.assertEqual(self.proof(f, candidate=dict(f['candidate'], car_size=f['tx_size'] - 1))['state'], 'rejected_candidate_size_mismatch')

    def test_receipt_hash_does_not_replace_raw_verification(self):
        f = make_block()
        block, receipt = MemoryArchive([f], self.index).read_slot(SLOT)
        receipt['car_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'RECEIPT_HASH'):
            corroborate_candidate(f['payer'], f['candidate'], block, receipt, Metadata)

    def test_slot_and_decoded_identity_are_checked(self):
        f = make_block()
        block, receipt = MemoryArchive([f], self.index).read_slot(SLOT)
        block['transactions'][0]['index'] += 1
        self.assertEqual(corroborate_candidate(f['payer'], f['candidate'], block, receipt, Metadata)['state'], 'rejected_decoded_transaction_mismatch')
        block['slot'] += 1
        self.assertEqual(corroborate_candidate(f['payer'], f['candidate'], block, receipt, Metadata)['state'], 'rejected_block_slot_mismatch')

    def test_provider_absence_is_unresolved_not_skipped(self):
        f = make_block()
        out = corroborate_candidate(f['payer'], f['candidate'], None, None, Metadata)
        self.assertEqual(out['state'], 'unresolved_provider_index_absent')
        self.assertTrue(out['not_proven_skipped'])

    def test_corroboration_does_not_impose_pump_launch_slot_floor(self):
        f = make_block(slot=0)
        block = parse_index_block(f['raw'], 0, Metadata)
        receipt = dict(raw=f['raw'], car_offset=1000, car_size=len(f['raw']),
                       car_sha256=hashlib.sha256(f['raw']).hexdigest())
        result = corroborate_candidate(f['payer'], f['candidate'], block, receipt, Metadata)
        self.assertEqual(result['state'], 'verified_success')
        self.assertEqual(result['slot'], 0)


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.f = make_block()
        self.second = make_block(slot=SLOT + 1)
        self.index = AddressIndex(self.root / 'address.sqlite3')
        self.addCleanup(self.index.close)
        self.reader = MemoryArchive([self.f, self.second], self.index)
        self.gsfa = FakeGsfa([self.f['candidate'], self.second['candidate']], self.f['payer'])
        self.guard = lambda extra=0: None
        self.path = self.root / 'history.sqlite3'
        self.history = GsfaHistory(self.path, self.gsfa, self.reader, self.guard)
        self.addCleanup(lambda: self.history.close())
        self.anchor = dict(root=encode(bytes([88]) * 32), slot=SLOT, tx_order=7, ix=0)
        self.job = self.history.register(self.f['payer'], self.anchor)

    def test_register_idempotent_and_anchor_not_cursor(self):
        self.assertEqual(self.job, self.history.register(self.f['payer'], self.anchor))
        self.history.step(self.job, page_size=1)
        self.assertEqual(self.history.status(self.job)['anchor'], self.anchor)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'IMMUTABLE_ANCHOR'):
            with self.history.db:
                self.history.db.execute("UPDATE gsfa_history_jobs SET anchor='{}' WHERE id=?", (self.job,))

    def test_restart_after_queue_preserves_pending_and_does_not_fetch_next_page(self):
        self.history.queue_page(self.job, page_size=2)
        self.history.close()
        self.history = GsfaHistory(self.path, self.gsfa, self.reader, self.guard)
        self.history.step(self.job)
        self.assertEqual(len(self.gsfa.calls), 1)
        self.assertEqual(self.history.status(self.job)['states']['pending'], 1)
        self.history.step(self.job)
        self.assertEqual(self.history.status(self.job)['state'], 'index_chain_drained')
        self.assertFalse(self.history.status(self.job)['complete_address_history'])

    def test_pagination_and_result_cursor_do_not_lose_same_signature_in_other_slot(self):
        self.history.step(self.job, page_size=1)
        first = self.history.results(self.job)
        self.history.step(self.job, page_size=1)
        second = self.history.results(self.job, after_id=first['next_after_id'])
        self.assertEqual(len(first['results']), 1)
        self.assertEqual(len(second['results']), 1)
        self.assertEqual([r['result']['slot'] for r in first['results'] + second['results']], [SLOT, SLOT + 1])

    def test_network_failure_retains_candidate_and_page_cursor_and_error_is_redacted(self):
        self.reader.error = RuntimeError('sensitive URL must not enter error journal')
        with self.assertRaises(RuntimeError):
            self.history.step(self.job, page_size=1)
        before = dict(self.history.db.execute('SELECT * FROM gsfa_history_progress WHERE job=?', (self.job,)).fetchone())
        attempt = dict(self.history.db.execute('SELECT * FROM gsfa_history_attempts').fetchone())
        self.assertEqual(attempt['state'], 'retryable_exception')
        self.assertEqual(attempt['error_type'], 'RuntimeError')
        self.assertNotIn('sensitive', json.dumps(attempt))
        self.history.step(self.job)
        after = dict(self.history.db.execute('SELECT * FROM gsfa_history_progress WHERE job=?', (self.job,)).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(len(self.gsfa.calls), 1)
        self.reader.error = None
        with self.history.db:
            self.history.db.execute('UPDATE gsfa_history_candidates SET next_at=0')
        self.history.step(self.job)
        self.assertEqual(len(self.history.results(self.job)['results']), 1)

    def test_process_death_attempt_retained_and_original_candidate_retried(self):
        self.history.queue_page(self.job)
        candidate = self.history.db.execute('SELECT id FROM gsfa_history_candidates ORDER BY id LIMIT 1').fetchone()[0]
        with self.history.db:
            self.history.db.execute('INSERT INTO gsfa_history_attempts(candidate,started,state) VALUES(?,?,?)', (candidate, time.time(), 'started'))
        self.history.step(self.job)
        self.assertEqual(self.history.status(self.job)['unresolved_attempt_outcomes'], 1)
        self.assertEqual(len(self.history.results(self.job)['results']), 1)

    def test_source_deferral_preserves_exact_retry_time_without_failure_claim(self):
        retry_at = int(time.time() * 1000) + 123000
        self.reader.error = ArchiveDeferred(retry_at)
        with self.assertRaises(ArchiveDeferred):
            self.history.step(self.job)
        attempt = self.history.db.execute('SELECT state,error_type FROM gsfa_history_attempts').fetchone()
        candidate = self.history.db.execute('SELECT next_at FROM gsfa_history_candidates ORDER BY id LIMIT 1').fetchone()
        self.assertEqual(attempt['state'], 'source_deferred')
        self.assertIsNone(attempt['error_type'])
        self.assertEqual(candidate['next_at'], retry_at / 1000)

    def test_blocking_archive_configuration_is_rejected(self):
        self.reader.wait = True
        with self.assertRaisesRegex(ValueError, 'NONBLOCKING'):
            GsfaHistory(self.root / 'other.sqlite3', self.gsfa, self.reader, self.guard)

    def test_explicit_unresolved_retry_keeps_old_result_and_anchor(self):
        self.reader.absent = True
        self.history.step(self.job, page_size=1)
        first = self.history.results(self.job)
        self.assertEqual(first['results'][0]['result']['state'], 'unresolved_provider_index_absent')
        self.reader.absent = False
        self.history.retry_unresolved(first['results'][0]['candidate_id'])
        self.history.step(self.job)
        new = self.history.results(self.job, after_id=first['next_after_id'])
        self.assertEqual(new['results'][0]['result']['state'], 'verified_success')
        self.assertEqual(len(self.history.results(self.job)['results']), 2)
        self.assertEqual(new['results'][0]['result']['anchor'], self.anchor)

    def test_requested_slot_range_is_explicit_not_full_history(self):
        job = self.history.register(self.f['payer'], self.anchor, first_slot=SLOT + 1)
        out = self.history.step(job, page_size=2)
        self.assertEqual(out['states']['outside_requested_slot_range'], 1)
        self.assertEqual(self.reader.calls, [SLOT + 1])
        self.assertFalse(out['all_epoch_transactions_proven'])

    def test_index_identity_change_cannot_resume_existing_job(self):
        self.gsfa.identity = 'changed'
        with self.assertRaisesRegex(ValueError, 'JOB_IDENTITY'):
            self.history.step(self.job)

    def test_multiple_epochs_share_journal_without_reusing_job_or_page_cursor(self):
        self.history.queue_page(self.job, page_size=1)
        original = dict(self.history.db.execute('SELECT * FROM gsfa_history_progress WHERE job=?', (self.job,)).fetchone())
        candidate = dict(self.f['candidate'], slot=1008*432000+120)
        other_index = FakeGsfa([candidate], self.f['payer'], epoch=1008)
        other = GsfaHistory(self.path, other_index, self.reader, self.guard)
        try:
            other_job = other.register(self.f['payer'], self.anchor)
            self.assertNotEqual(other_job, self.job)
            with self.assertRaisesRegex(ValueError, 'JOB_IDENTITY'):
                other.status(self.job)
            other.queue_page(other_job)
            self.assertEqual(other.status(other_job)['epoch'], 1008)
            self.assertEqual(other.status(other_job)['anchor'], self.anchor)
            self.assertEqual(other.status(other_job)['states'], {'pending':1})
            self.assertFalse(other.status(other_job)['complete_address_history'])
            self.assertEqual(dict(self.history.db.execute('SELECT * FROM gsfa_history_progress WHERE job=?', (self.job,)).fetchone()), original)
            self.assertEqual(self.reader.calls, [])
        finally:
            other.close()

    def test_page_identity_and_no_progress_rejected_without_cursor_commit(self):
        original = self.gsfa.page
        def broken(*args, **kwargs):
            page = original(*args, **kwargs)
            page['wallet'] = encode(bytes([55]) * 32)
            return page
        with patch.object(self.gsfa, 'page', broken):
            with self.assertRaisesRegex(ValueError, 'PAGE_IDENTITY'):
                self.history.queue_page(self.job)
        self.assertEqual(self.history.status(self.job)['pages'], 0)

    def test_duplicate_page_candidates_do_not_duplicate_verification(self):
        self.gsfa.candidates = [self.f['candidate'], self.f['candidate']]
        self.history.step(self.job)
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(len(self.history.results(self.job)['results']), 1)

    def test_same_location_changed_flags_roll_back_page_and_cursor(self):
        self.gsfa.candidates = [self.f['candidate'], dict(self.f['candidate'], index_success=False)]
        with self.assertRaisesRegex(ValueError, 'CANDIDATE_CHANGED'):
            self.history.queue_page(self.job)
        self.assertEqual(self.history.status(self.job)['pages'], 0)
        self.assertEqual(self.history.status(self.job)['states'], {})

    def test_existing_deadline_and_storage_guard_are_enforced_before_new_work(self):
        def fail(*args):
            raise ValueError('ORIGINAL_DEADLINE')
        self.gsfa.budget.check = fail
        with self.assertRaisesRegex(ValueError, 'ORIGINAL_DEADLINE'):
            self.history.step(self.job)
        self.assertEqual(self.reader.calls, [])


if __name__ == '__main__':
    unittest.main()
