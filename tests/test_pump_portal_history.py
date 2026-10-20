import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import zlib

from economic_machine.pump_research.address_index import AddressIndex
from economic_machine.pump_research.portal_history import (
    PortalHistory, MAX_RESPONSE, SOURCE_BYTES, PRIOR_PROBE_BYTES, WINDOW_SLOTS,
    corroborate_candidates, parse_page, query, read_cache, sha, portal_network, PortalDeferred)
from economic_machine.pump_research.store import canonical, encode
from economic_machine.pump_research.wallet_map import Graph

WALLET = encode(bytes([3]) * 32)
OTHER = encode(bytes([4]) * 32)
ROOT = encode(bytes([5]) * 32)
SIG = encode(bytes([6]) * 64)
SLOT = 300_000_000


def transaction(**changes):
    value = dict(transactionIndex=2, signatures=[SIG], accountKeys=[WALLET],
        loadedAddresses=dict(readonly=[], writable=[]), err=None)
    value.update(changes)
    return value


def line(slot=SLOT, txs=None):
    return canonical(dict(header=dict(number=slot, hash='a' * 44),
        transactions=[transaction()] if txs is None else txs)).encode() + b'\n'


class PageTests(unittest.TestCase):
    def parse(self, raw, **kwargs):
        return parse_page(raw, WALLET, SLOT, SLOT + 9999, **kwargs)

    def test_query_has_only_bounded_public_selector(self):
        body = query(WALLET, SLOT, SLOT + 9999)
        self.assertEqual(body['type'], 'solana')
        self.assertEqual(body['transactions'], [dict(mentionsAccount=[WALLET])])
        with self.assertRaisesRegex(ValueError, 'WINDOW'):
            query(WALLET, SLOT, SLOT + 10000)

    def test_advance_last_header_not_requested_end(self):
        result = self.parse(line())
        self.assertEqual(result['next_slot'], SLOT + 1)
        self.assertEqual(result['candidates'][0]['evidence_tier'], 'provider_index_claim')

    def test_truncated_last_record_not_consumed(self):
        result = self.parse(line() + line(SLOT + 2)[:40], truncated=True)
        self.assertEqual(result['next_slot'], SLOT + 1)
        self.assertEqual(len(result['headers']), 1)

    def test_complete_no_newline_at_eof(self):
        self.assertEqual(self.parse(line().rstrip(b'\n'))['next_slot'], SLOT + 1)

    def test_204_and_empty_have_explicit_unadvanced_gap(self):
        for status in (200, 204):
            result = self.parse(b'', http_status=status)
            self.assertTrue(result['empty'])
            self.assertEqual(result['next_slot'], SLOT)

    def test_unordered_and_duplicate_headers_rejected(self):
        for raw in (line(SLOT + 1) + line(), line() + line()):
            with self.assertRaisesRegex(ValueError, 'ORDER'):
                self.parse(raw)

    def test_provider_wrong_wallet_rejected(self):
        with self.assertRaisesRegex(ValueError, 'WRONG_WALLET'):
            self.parse(line(txs=[transaction(accountKeys=[OTHER])]))

    def test_loaded_wallet_membership_supported(self):
        result = self.parse(line(txs=[transaction(accountKeys=[OTHER], loadedAddresses=dict(readonly=[WALLET], writable=[]))]))
        self.assertEqual(len(result['candidates']), 1)

    def test_failed_transaction_is_candidate_not_value_transfer(self):
        result = self.parse(line(txs=[transaction(err={'InstructionError': [0, 'x']})]))
        self.assertFalse(result['candidates'][0]['provider_success'])

    def test_malformed_final_json_not_silently_skipped(self):
        with self.assertRaisesRegex(ValueError, 'NDJSON'):
            self.parse(line() + b'{bad}')

    def test_provider_skipped_numbers_are_not_consensus_proof(self):
        result = self.parse(line(txs=[]) + line(SLOT + 100, []))
        self.assertEqual(result['next_slot'], SLOT + 101)
        self.assertFalse(result['provider_filtered_gaps_verified'])

    def test_candidate_limit_never_acknowledges_unprocessed_block(self):
        txs = [transaction(transactionIndex=i, signatures=[encode(i.to_bytes(2, 'little') + bytes(62))]) for i in range(128)]
        result = self.parse(line(txs=txs) + line(SLOT + 1))
        self.assertEqual(result['next_slot'], SLOT + 1)
        self.assertEqual(len(result['candidates']), 128)


class CorroborationTests(unittest.TestCase):
    def setUp(self):
        self.candidates = parse_page(line(), WALLET, SLOT, SLOT)['candidates']
        self.tx = dict(signature=SIG, index=2, account_keys=[WALLET], success=True)
        self.block = dict(slot=SLOT, transactions=[self.tx], decode_errors=[])

    def test_matching_raw_identity_membership_and_status(self):
        result = corroborate_candidates(self.candidates, self.block, WALLET, SLOT)
        self.assertEqual(result['state'], 'portal_raw_corroborated')
        self.assertFalse(result['independent_consensus_verified'])

    def test_wrong_raw_wallet(self):
        self.tx['account_keys'] = [OTHER]
        result = corroborate_candidates(self.candidates, self.block, WALLET, SLOT)
        self.assertEqual(result['state'], 'portal_raw_mismatch')
        self.assertEqual(result['successful_transactions'], 0)

    def test_failed_raw_does_not_corroborate_provider_success(self):
        self.tx['success'] = False
        result = corroborate_candidates(self.candidates, self.block, WALLET, SLOT)
        self.assertEqual(result['matches'][0]['state'], 'raw_status_mismatch')

    def test_verified_failed_transaction_never_counted_as_success(self):
        self.tx['success'] = False
        self.candidates[0]['provider_success'] = False
        result = corroborate_candidates(self.candidates, self.block, WALLET, SLOT)
        self.assertEqual(result['verified_transactions'], 1)
        self.assertEqual(result['successful_transactions'], 0)

    def test_absent_provider_slot_not_proven_skipped(self):
        result = corroborate_candidates(self.candidates, None, WALLET, SLOT)
        self.assertTrue(result['not_proven_skipped'])

    def test_signature_or_order_mismatch_not_corroborated(self):
        self.tx['index'] = 1
        result = corroborate_candidates(self.candidates, self.block, WALLET, SLOT)
        self.assertEqual(result['matches'][0]['state'], 'raw_transaction_order_mismatch')
        self.tx['signature'] = encode(bytes([7]) * 64)
        self.assertEqual(corroborate_candidates(self.candidates, self.block, WALLET, SLOT)['verified_transactions'], 0)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); (self.base / 'evidence').mkdir()
        self.g = Graph(self.base); self.addCleanup(self.g.close)
        self.adapter = PortalHistory(self.g)
        self.reserved = []
        self.scope = SimpleNamespace(guard=lambda *a, **k: None, reserve=lambda n, p: self.reserved.append((n, p)))
        self.index = AddressIndex(self.g.out / 'address-index.sqlite3'); self.addCleanup(self.index.close)
        block = dict(slot=SLOT, time=1736000000, transaction_nodes=1, decode_errors=[], transactions=[dict(
            signature=SIG, index=2, success=True, account_keys=[WALLET], transfers=[], activity=[], launches=[])])
        self.index.ingest(block, dict(car_sha256='b' * 64, transaction_signatures_verified=True))
        with self.g.db:
            self.g.db.execute('INSERT INTO roots VALUES(?,?)', (ROOT, '{}'))
            self.parent = self.g.enqueue(ROOT, WALLET, SLOT, 1736000000000, 0, 'local_forward', after_order=(2, 1))
            self.g.set('portal_finalized_head', dict(number=SLOT + 10000, hash='c' * 44, at_ms=1792054000000))

    def job(self):
        self.adapter.seed(self.scope)
        return dict(self.g.db.execute("SELECT * FROM frontier WHERE source='portal_forward'").fetchone())

    def mocked_fetch(self, raw, status=200):
        digest = sha(raw)
        (self.adapter.folder / (digest + '.ndjson.zlib')).write_bytes(zlib.compress(raw))
        return raw, dict(sha256=digest, truncated=False, http_status=status)

    def test_seed_restart_preserves_anchor_and_single_pending_root(self):
        job = self.job()
        PortalHistory(self.g).seed(self.scope)
        self.assertEqual(self.g.db.execute("SELECT count(*) FROM frontier WHERE source='portal_forward'").fetchone()[0], 1)
        meta = json.loads(self.g.db.execute('SELECT body FROM portal_jobs WHERE job=?', (job['id'],)).fetchone()[0])
        self.assertEqual(meta['anchor']['tx_order'], 2)
        self.assertEqual(meta['parent_job'], self.parent)

    def test_explicit_wallet_discovery_needs_no_invented_membership(self):
        value = self.adapter.seed_wallet(ROOT, OTHER, self.scope)
        self.assertEqual(value['start_slot'], SLOT + 10000 - 216000)
        self.assertFalse(value['verified_funding_anchor'])
        self.assertFalse(value['whole_wallet_history'])
        saved = dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?', (value['job_id'],)).fetchone())
        self.assertIsNone(self.g.anchor(saved))
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)

    def test_discovery_replay_preserves_cursor(self):
        value = self.adapter.seed_wallet(ROOT, OTHER, self.scope, start=SLOT, end=SLOT + 3)
        with self.g.db:
            self.g.db.execute('UPDATE frontier SET cursor=? WHERE id=?', ('{"next_slot":300000001,"ordinal":1}', value['job_id']))
        again = self.adapter.seed_wallet(ROOT, OTHER, self.scope, start=SLOT, end=SLOT + 3)
        self.assertEqual(value['job_id'], again['job_id'])
        self.assertEqual(json.loads(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?', (again['job_id'],)).fetchone()[0])['next_slot'], SLOT + 1)

    def test_discovery_cannot_extend_pinned_provider_head(self):
        with self.assertRaisesRegex(ValueError, 'DISCOVERY_WINDOW'):
            self.adapter.seed_wallet(ROOT, OTHER, self.scope, start=SLOT, end=SLOT + 10001)

    def test_unobserved_wallet_or_unpinned_head_cannot_seed(self):
        with self.g.db:
            self.g.set('portal_finalized_head', None)
        self.assertEqual(self.adapter.seed(self.scope)['seeded'], 0)

    def test_provider_page_creates_candidates_but_no_graph_edges(self):
        job = self.job()
        with patch.object(self.adapter, 'fetch', return_value=self.mocked_fetch(line())):
            result = self.adapter.run(job, self.scope)
        self.assertEqual(result['candidate_transactions'], 1)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)
        self.assertEqual(self.g.db.execute("SELECT count(*) FROM frontier WHERE source='portal_verify'").fetchone()[0], 1)

    def test_stale_job_reload_does_not_rewind_cursor_or_duplicate_page(self):
        job = self.job()
        def fetch(body, scope):
            return self.mocked_fetch(line(body['fromBlock'], []))
        with patch.object(self.adapter, 'fetch', side_effect=fetch):
            self.adapter.run(job, self.scope)
            result = PortalHistory(self.g)
            with patch.object(result, 'fetch', side_effect=fetch):
                result.run(job, self.scope)
        cursor = json.loads(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?', (job['id'],)).fetchone()[0])
        self.assertEqual(cursor['next_slot'], SLOT + 2)

    def test_duplicate_old_provider_response_rejected_without_cursor_change(self):
        job = self.job()
        with patch.object(self.adapter, 'fetch', return_value=self.mocked_fetch(line())):
            self.adapter.run(job, self.scope)
            with self.assertRaisesRegex(ValueError, 'BLOCK_ORDER'):
                self.adapter.run(job, self.scope)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_pages').fetchone()[0], 1)

    def test_empty_result_persists_gap_no_later_window_jump(self):
        job = self.job()
        with patch.object(self.adapter, 'fetch', return_value=self.mocked_fetch(b'', 204)):
            result = self.adapter.run(job, self.scope)
        self.assertEqual(result['state'], 'portal_waiting_gap')
        self.assertEqual(result['next_slot'], SLOT)

    def test_fixed_source_budget_survives_restart(self):
        with self.g.db:
            self.g.db.execute('INSERT INTO portal_source_io(bytes,request,at,status,body) VALUES(?,?,?,?,?)',
                (SOURCE_BYTES - PRIOR_PROBE_BYTES, 'old', 0, 'reserved', '{}'))
        with self.assertRaisesRegex(ValueError, 'IO_LIMIT'):
            PortalHistory(self.g)._reserve(self.scope, 'request')
        self.assertEqual(self.reserved, [])

    def test_metadata_reservations_and_prior_probes_included(self):
        folder = self.base / 'evidence/recent50-v1'; folder.mkdir()
        path = folder / 'portal-metadata-ledger.json'
        path.write_text(canonical(dict(schema='economic-machine.portal-metadata-ledger.v1', requests=[dict(reserved_bytes=65537)])))
        self.adapter._reserve(self.scope, 'request')
        self.assertEqual(self.adapter.snapshot()['source_io_reserved_bytes'], PRIOR_PROBE_BYTES + 65537 + MAX_RESPONSE + 1)
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'LEDGER_SCHEMA'):
            self.adapter._reserve(self.scope, 'other')

    def test_launch_ledger_is_counted_with_metadata_and_shared_eight_mib_headroom(self):
        path = self.g.out / 'portal-launch-ledger.json'
        path.write_text(canonical(dict(schema='economic-machine.portal-launch-ledger.v1', requests=[dict(reserved_bytes=4097)])))
        self.assertEqual(self.adapter.snapshot()['source_io_reserved_bytes'], PRIOR_PROBE_BYTES + 4097)
        with self.g.db:
            self.g.db.execute('INSERT INTO portal_source_io(bytes,request,at,status,body) VALUES(?,?,?,?,?)',
                (SOURCE_BYTES - PRIOR_PROBE_BYTES - 8 * 1024 ** 2, 'old', 0, 'reserved', '{}'))
        with self.assertRaisesRegex(ValueError, 'IO_LIMIT'):
            self.adapter._reserve(self.scope, 'request')
        path.write_text(canonical(dict(requests=[])))
        with self.assertRaisesRegex(ValueError, 'LEDGER_SCHEMA'):
            self.adapter.snapshot()

    def test_failed_scope_reservation_keeps_conservative_local_reservation(self):
        self.scope.reserve = lambda *a: (_ for _ in ()).throw(ValueError('CORE_ADDITIONAL_IO_LIMIT'))
        with self.assertRaisesRegex(ValueError, 'CORE_'):
            self.adapter._reserve(self.scope, 'request')
        self.assertEqual(self.adapter.snapshot()['wallet_query_reserved_bytes'], MAX_RESPONSE + 1)

    def test_http429_retry_after_and_reservation_retained(self):
        error = urllib.error.HTTPError('https://portal.sqd.dev/', 429, 'rate limit', {'Retry-After': '45'}, None)
        with patch('economic_machine.pump_research.portal_history.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(urllib.error.HTTPError):
                self.adapter.fetch(query(WALLET, SLOT, SLOT), self.scope)
        row = self.g.db.execute('SELECT status,body FROM portal_source_io').fetchone()
        self.assertEqual(row[0], 'http_error')
        self.assertEqual(json.loads(row[1])['retry_after'], '45')
        self.assertEqual(self.adapter.snapshot()['wallet_query_reserved_bytes'], MAX_RESPONSE + 1)

    def test_shared_529_retry_after_persists_for_metadata_and_wallet_callers(self):
        error = urllib.error.HTTPError('https://portal.sqd.dev/', 529, 'overloaded', {'Retry-After': '300'}, None)
        with patch('economic_machine.pump_research.portal_history.time.time', return_value=100):
            with self.assertRaises(urllib.error.HTTPError):
                with portal_network(self.base, self.scope):
                    raise error
        state = json.loads((self.g.out / 'portal-network-state.json').read_bytes())
        self.assertEqual(state['not_before'], 400)
        self.assertEqual(state['status'], 529)
        self.assertTrue(state['cursor_preserved'])
        self.assertTrue(state['failed_reservation_retained'])

    def test_shared_backoff_obeys_scope_deadline_before_dispatch(self):
        (self.g.out / 'portal-network-state.json').write_text(canonical(dict(not_before=1000, attempts=2)))
        calls = []
        def guard(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError('CORE_DEADLINE')
        scope = SimpleNamespace(guard=guard)
        with patch('economic_machine.pump_research.portal_history.time.time', return_value=100), patch('economic_machine.pump_research.portal_history.time.sleep'):
            with self.assertRaisesRegex(ValueError, 'CORE_DEADLINE'):
                with portal_network(self.base, scope):
                    self.fail('request admitted before shared cooldown')

    def test_shared_backoff_accepts_http_date_retry_after(self):
        error = urllib.error.HTTPError('https://portal.sqd.dev/', 503, 'overloaded', {'Retry-After': 'Thu, 01 Jan 1970 00:10:00 GMT'}, None)
        with patch('economic_machine.pump_research.portal_history.time.time', return_value=100):
            with self.assertRaises(urllib.error.HTTPError):
                with portal_network(self.base, self.scope):
                    raise error
        state = json.loads((self.g.out / 'portal-network-state.json').read_bytes())
        self.assertEqual(state['not_before'], 600)

    def test_success_only_resets_backoff_after_admission(self):
        with patch('economic_machine.pump_research.portal_history.time.time', return_value=100):
            with portal_network(self.base, self.scope):
                pass
        state = json.loads((self.g.out / 'portal-network-state.json').read_bytes())
        self.assertEqual(state['not_before'], 101)
        self.assertEqual(state['attempts'], 0)

    def test_nonblocking_cooldown_defers_without_http_reservation_or_cursor_change(self):
        job = self.job()
        state = dict(not_before=1000.0001, attempts=3, status=529)
        path = self.g.out / 'portal-network-state.json'
        path.write_text(canonical(state)); prior = path.read_bytes()
        before = tuple(self.g.db.execute('SELECT state,cursor FROM frontier WHERE id=?', (job['id'],)).fetchone())
        with patch('economic_machine.pump_research.portal_history.time.time', return_value=100), \
                patch('economic_machine.pump_research.portal_history.time.sleep') as sleep, \
                patch('economic_machine.pump_research.portal_history.urllib.request.build_opener') as opener:
            with self.assertRaises(PortalDeferred) as caught:
                self.adapter.run(job, self.scope)
        self.assertEqual(caught.exception.retry_at_ms, 1000001)
        self.assertEqual(caught.exception.reason, 'source_cooldown')
        sleep.assert_not_called(); opener.assert_not_called()
        self.assertEqual(self.reserved, [])
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM portal_source_io').fetchone()[0], 0)
        self.assertEqual(tuple(self.g.db.execute('SELECT state,cursor FROM frontier WHERE id=?', (job['id'],)).fetchone()), before)
        self.assertEqual(path.read_bytes(), prior)

    def test_nonblocking_lock_busy_defers_without_sleep_or_unlocking_other_owner(self):
        with patch('economic_machine.pump_research.portal_history.fcntl.flock', side_effect=BlockingIOError) as flock, \
                patch('economic_machine.pump_research.portal_history.time.time', return_value=100), \
                patch('economic_machine.pump_research.portal_history.time.sleep') as sleep:
            with self.assertRaises(PortalDeferred) as caught:
                with portal_network(self.base, self.scope, wait=False):
                    self.fail('request must not be admitted while lock is busy')
        self.assertEqual(caught.exception.retry_at_ms, 101000)
        self.assertEqual(caught.exception.reason, 'lock_busy')
        self.assertEqual(flock.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse((self.g.out / 'portal-network-state.json').exists())

    def test_nonblocking_cooldown_releases_lock_for_next_admission(self):
        path = self.g.out / 'portal-network-state.json'
        path.write_text(canonical(dict(not_before=200, attempts=1)))
        with patch('economic_machine.pump_research.portal_history.time.time', return_value=100):
            with self.assertRaises(PortalDeferred):
                with portal_network(self.base, self.scope, wait=False):
                    self.fail('cooldown must defer')
        with patch('economic_machine.pump_research.portal_history.time.time', return_value=201):
            with portal_network(self.base, self.scope, wait=False):
                pass
        self.assertEqual(json.loads(path.read_bytes())['not_before'], 202)

    def test_launch_resolution_defers_before_reserving_or_mutating_ledger(self):
        from economic_machine.pump_research.launch_resolver import LaunchResolver
        resolver = LaunchResolver(self.g)
        state = dict(not_before=500, attempts=2, status=503)
        path = self.g.out / 'portal-network-state.json'; path.write_text(canonical(state))
        with patch('economic_machine.pump_research.portal_history.time.time', return_value=100), \
                patch('economic_machine.pump_research.portal_history.time.sleep') as sleep, \
                patch('economic_machine.pump_research.launch_resolver.urllib.request.build_opener') as opener:
            with self.assertRaises(PortalDeferred) as caught:
                resolver._fetch(dict(mint=ROOT, created_ms=1736000000000), self.scope)
        self.assertEqual(caught.exception.retry_at_ms, 500000)
        sleep.assert_not_called(); opener.assert_not_called()
        self.assertEqual(self.reserved, [])
        self.assertFalse(resolver.path.exists())
        self.assertEqual(json.loads(path.read_bytes()), state)

    def test_verify_rechecks_raw_page_hash_before_archive_call(self):
        job = self.job()
        raw, receipt = self.mocked_fetch(line())
        with patch.object(self.adapter, 'fetch', return_value=(raw, receipt)):
            self.adapter.run(job, self.scope)
        verify = dict(self.g.db.execute("SELECT * FROM frontier WHERE source='portal_verify'").fetchone())
        (self.adapter.folder / (receipt['sha256'] + '.ndjson.zlib')).write_bytes(zlib.compress(b'changed'))
        with self.assertRaisesRegex(ValueError, 'CACHE_HASH'):
            self.adapter.run(verify, self.scope)

    def test_compressed_cache_bomb_rejected(self):
        path = self.adapter.folder / 'bad.zlib'; raw = b'x' * (MAX_RESPONSE + 2)
        path.write_bytes(zlib.compress(raw))
        with self.assertRaisesRegex(ValueError, 'CACHE_HASH'):
            read_cache(path, sha(raw))

    def test_raw_verification_yields_archive_cooldown_without_advancing_job(self):
        from economic_machine.pump_research.raw_history import ArchiveDeferred
        import time
        job = self.job()
        with patch.object(self.adapter, 'fetch', return_value=self.mocked_fetch(line())):
            self.adapter.run(job, self.scope)
        verify = dict(self.g.db.execute("SELECT * FROM frontier WHERE source='portal_verify'").fetchone())
        folder = self.g.out / 'archive-ranges'; folder.mkdir(exist_ok=True)
        state = canonical(dict(not_before=time.time()+120, rate_limit_attempts=3))
        (folder / 'network-state.json').write_text(state)
        self.scope.reserved = lambda: 0
        self.scope.grant = {'additional_io_bytes': 12*1024**3}
        with patch('economic_machine.pump_research.cli.proto', return_value=(None, None)), patch('economic_machine.pump_research.raw_history.get') as get, patch('economic_machine.pump_research.raw_history.time.sleep') as sleep:
            with self.assertRaises(ArchiveDeferred):
                self.adapter.run(verify, self.scope)
        saved = dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?', (verify['id'],)).fetchone())
        self.assertEqual(saved, verify)
        self.assertEqual((folder / 'network-state.json').read_text(), state)
        get.assert_not_called(); sleep.assert_not_called()

    def test_raw_corroboration_wakes_indexed_job_preserving_cursor(self):
        job = self.job()
        with patch.object(self.adapter, 'fetch', return_value=self.mocked_fetch(line())):
            self.adapter.run(job, self.scope)
        with self.g.db:
            idx = self.g.enqueue(ROOT, WALLET, SLOT, 1736000000000, 0, 'indexed_forward', after_order=(2, 1))
            self.g.db.execute("UPDATE frontier SET state='indexed_source_exhausted',cursor=? WHERE id=?", ('{"activity":17,"transfer":9}', idx))
        verify = dict(self.g.db.execute("SELECT * FROM frontier WHERE source='portal_verify'").fetchone())
        block = dict(slot=SLOT, transactions=[dict(signature=SIG, index=2, account_keys=[WALLET], success=True)], decode_errors=[])
        with patch('economic_machine.pump_research.cli.proto', return_value=(None, None)), patch('economic_machine.pump_research.raw_history.ScopedArchive.read_slot', return_value=(block, dict(car_sha256='d' * 64))):
            result = self.adapter.run(verify, self.scope)
        saved = self.g.db.execute('SELECT state,cursor FROM frontier WHERE id=?', (idx,)).fetchone()
        self.assertEqual(result['successful_transactions'], 1)
        self.assertEqual(saved[0], 'pending')
        self.assertEqual(json.loads(saved[1]), dict(activity=17, transfer=9))
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)

    def test_discovery_raw_results_wake_only_existing_slot_zero_reader(self):
        value = self.adapter.seed_wallet(ROOT, WALLET, self.scope, start=SLOT, end=SLOT + 3)
        job = dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?', (value['job_id'],)).fetchone())
        with patch.object(self.adapter, 'fetch', return_value=self.mocked_fetch(line())):
            self.adapter.run(job, self.scope)
        with self.g.db:
            idx = self.g.enqueue(ROOT, WALLET, 0, None, 0, 'indexed_forward')
            self.g.db.execute("UPDATE frontier SET state='indexed_source_exhausted',cursor=? WHERE id=?", ('{"activity":17,"transfer":9}', idx))
        verify = dict(self.g.db.execute("SELECT * FROM frontier WHERE source='portal_verify'").fetchone())
        block = dict(slot=SLOT, transactions=[dict(signature=SIG, index=2, account_keys=[WALLET], success=True)], decode_errors=[])
        with patch('economic_machine.pump_research.cli.proto', return_value=(None, None)), patch('economic_machine.pump_research.raw_history.ScopedArchive.read_slot', return_value=(block, dict(car_sha256='d' * 64))):
            self.adapter.run(verify, self.scope)
        saved = self.g.db.execute('SELECT state,cursor FROM frontier WHERE id=?', (idx,)).fetchone()
        self.assertEqual(saved[0], 'pending')
        self.assertEqual(json.loads(saved[1]), dict(activity=17, transfer=9))


if __name__ == '__main__':
    unittest.main()
