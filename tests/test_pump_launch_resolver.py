from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from economic_machine.pump_research.cohort import ORIGINAL_DEADLINE_MS
from economic_machine.pump_research.launch_resolver import CAP, RESPONSE_LIMIT, SCHEMA, LaunchResolver
from economic_machine.pump_research.store import atomic, canonical, encode
from economic_machine.pump_research.wallet_map import Graph


AS_OF = 1792055000000


def key(number):
    return encode(number.to_bytes(32, 'big'))


class Reply:
    status = 200

    def __init__(self, raw):
        self.raw = raw
        self.limits = []

    def read(self, maximum):
        self.limits.append(maximum)
        return self.raw[:maximum]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base/'evidence').mkdir()
        self.graph = Graph(self.base)
        self.addCleanup(self.graph.close)
        self.resolver = LaunchResolver(self.graph)
        self.reservations = []
        self.scope = SimpleNamespace(grant=dict(deadline_ms=ORIGINAL_DEADLINE_MS),
            guard=lambda *args, **kwargs: None,
            reserve=lambda size, purpose: self.reservations.append((size, purpose)))
        self.clock = patch('economic_machine.pump_research.launch_resolver.time.time_ns', return_value=AS_OF*1000000)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with self.graph.db:
            self.graph.set('portal_finalized_head', dict(number=456000000, hash='synthetic-provider-head', at_ms=AS_OF))

    def root(self, number=1, cohort=True, creator=True, wallet=False):
        mint = key(number)
        value = dict(mint=mint, symbol='TEST', created_ms=AS_OF-1000,
                     creator=key(number+1000) if creator else None)
        if cohort:
            value['cohort_kind'] = 'recent30d_current_marketcap'
        if wallet:
            value['root_kind'] = 'wallet'
        with self.graph.db:
            self.graph.db.execute('INSERT INTO roots VALUES(?,?)', (mint, canonical(value)))
        return value

    def recover(self, root, cursor='null'):
        with self.graph.db:
            ident = self.graph.enqueue(root['mint'], root['creator'] or root['mint'], None,
                                       root['created_ms'], 0, 'recover_launch')
            self.graph.db.execute('UPDATE frontier SET cursor=? WHERE id=?', (cursor, ident))
        return ident

    def resolver_job(self, mint):
        return dict(self.graph.db.execute("SELECT * FROM frontier WHERE root=? AND source='portal_resolve_launch'", (mint,)).fetchone())

    def cached(self, root, slot=455000000):
        folder = self.base/'evidence/recent50-v1'
        folder.mkdir(exist_ok=True)
        raw = canonical(dict(block_number=slot)).encode()
        name = 'portal-meta-0001.json'
        (folder/name).write_bytes(raw)
        atomic(folder/'portal-metadata-ledger.json', dict(schema='economic-machine.portal-metadata-ledger.v1', requests=[
            dict(id=1, endpoint='/timestamps/'+str(root['created_ms']//1000)+'/block', request_body=None,
                 reserved_bytes=65537, status=200, sha256=hashlib.sha256(raw).hexdigest(), raw_file=name)]))
        atomic(folder/'launch-anchors.json', {root['mint']:dict(slot=slot, created_ms=root['created_ms'],
            source='portal_timestamp_provider_claim', start=-999, end=9999999999, canonical_verified=False)})
        return folder, raw

    def run_with_raw(self, job, raw):
        reply = Reply(raw)
        with patch('economic_machine.pump_research.launch_resolver.portal_network', return_value=nullcontext()), \
                patch('economic_machine.pump_research.launch_resolver.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = reply
            result = self.resolver.run(job, self.scope)
        return result, reply

    def test_seed_only_new_cohorts_and_preserves_recovery_cursor(self):
        old = self.root(1, cohort=False)
        fresh = self.root(2)
        wallet = self.root(3, wallet=True)
        old_job = self.recover(old, '{"old":123}')
        fresh_job = self.recover(fresh, '{"time_search":{"samples":[[1,2]]}}')
        wallet_job = self.recover(wallet)
        original = tuple(self.graph.db.execute('SELECT * FROM frontier WHERE id=?', (old_job,)).fetchone())
        result = self.resolver.seed(self.scope)
        self.assertEqual(result['queued'], 1)
        self.assertEqual(tuple(self.graph.db.execute('SELECT * FROM frontier WHERE id=?', (old_job,)).fetchone()), original)
        row = self.graph.db.execute('SELECT state,cursor FROM frontier WHERE id=?', (fresh_job,)).fetchone()
        self.assertEqual(tuple(row), ('waiting_for_launch_anchor', '{"time_search":{"samples":[[1,2]]}}'))
        self.assertEqual(self.graph.db.execute('SELECT state FROM frontier WHERE id=?', (wallet_job,)).fetchone()[0], 'pending')
        self.resolver.seed(self.scope)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM frontier WHERE source='portal_resolve_launch'").fetchone()[0], 1)

    def test_success_wakes_recovery_without_cursor_reset_and_keeps_receipt(self):
        root = self.root()
        cursor = '{"time_search":{"low":300000000,"high":456000000}}'
        recover = self.recover(root, cursor)
        self.resolver.seed(self.scope)
        result, reply = self.run_with_raw(self.resolver_job(root['mint']), b'{"block_number":455999999}')
        self.assertEqual(reply.limits, [RESPONSE_LIMIT+1])
        self.assertEqual(result['state'], 'launch_anchor_ready')
        self.assertFalse(result['launch_verified'])
        self.assertFalse(result['canonical_verified'])
        self.assertTrue(result['actual_car_required'])
        self.assertEqual(self.reservations[0][0], RESPONSE_LIMIT+1)
        row = self.graph.db.execute('SELECT state,cursor FROM frontier WHERE id=?', (recover,)).fetchone()
        self.assertEqual(tuple(row), ('pending', cursor))
        anchor = self.graph.state('launch_anchor:'+root['mint'])
        self.assertEqual(anchor['end'], 456000000)
        ledger = json.loads(self.resolver.path.read_bytes())
        receipt = ledger['requests'][0]
        self.assertTrue(receipt['validated'])
        self.assertEqual(hashlib.sha256((self.resolver.folder/receipt['raw_file']).read_bytes()).hexdigest(), receipt['sha256'])
        before = canonical(anchor)
        self.resolver.seed(self.scope)
        self.assertEqual(canonical(self.graph.state('launch_anchor:'+root['mint'])), before)

    def test_missing_creator_remains_unknown_with_mint_lookup_only(self):
        root = self.root(creator=False)
        self.resolver.seed(self.scope)
        job = self.resolver_job(root['mint'])
        self.assertEqual(job['wallet'], root['mint'])
        self.run_with_raw(job, b'{"block_number":455000000}')
        stored = json.loads(self.graph.db.execute('SELECT body FROM roots WHERE mint=?', (root['mint'],)).fetchone()[0])
        self.assertIsNone(stored['creator'])
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM edges WHERE kind='provider_creator_claim'").fetchone()[0], 0)
        self.assertTrue(self.graph.state('launch_recovery_lookup:'+root['mint'])['wallet_is_mint_lookup'])
        self.assertEqual(self.graph.db.execute("SELECT wallet FROM frontier WHERE source='recover_launch'").fetchone()[0], root['mint'])

    def test_cached_anchor_raw_hash_validation_and_clamped_window(self):
        root = self.root()
        with self.graph.db:
            self.graph.set('portal_finalized_head', dict(number=7, hash='synthetic-low-head'))
        folder, raw = self.cached(root, slot=3)
        recover = self.recover(root, '{"keep":1}')
        with self.graph.db:
            self.graph.db.execute("UPDATE frontier SET state='waiting_for_launch_anchor' WHERE id=?", (recover,))
        with patch('economic_machine.pump_research.launch_resolver.urllib.request.build_opener') as opener:
            self.assertEqual(self.resolver.seed(self.scope)['cached'], 1)
        opener.assert_not_called()
        anchor = self.graph.state('launch_anchor:'+root['mint'])
        self.assertEqual((anchor['start'], anchor['end']), (0, 7))
        self.assertEqual(anchor['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(anchor['receipt']['metadata_ledger_sha256'], hashlib.sha256((folder/'portal-metadata-ledger.json').read_bytes()).hexdigest())
        self.assertEqual(self.reservations, [])
        self.assertEqual(tuple(self.graph.db.execute('SELECT state,cursor FROM frontier WHERE id=?', (recover,)).fetchone()), ('pending', '{"keep":1}'))

    def test_corrupted_cached_metadata_is_not_an_anchor(self):
        root = self.root()
        folder, _ = self.cached(root)
        (folder/'portal-meta-0001.json').write_bytes(b'{"block_number":1}')
        with self.assertRaisesRegex(ValueError, 'LAUNCH_METADATA_HASH'):
            self.resolver.seed(self.scope)
        self.assertIsNone(self.graph.state('launch_anchor:'+root['mint']))

    def test_caps_reject_before_network_and_preserve_ledger(self):
        root = self.root()
        self.resolver.seed(self.scope)
        atomic(self.resolver.path, dict(schema=SCHEMA, requests=[dict(id=1, reserved_bytes=CAP, status='reserved')]))
        before = self.resolver.path.read_bytes()
        with patch('economic_machine.pump_research.launch_resolver.portal_network', return_value=nullcontext()), \
                patch('economic_machine.pump_research.launch_resolver.urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'LAUNCH_LEDGER_CAP'):
                self.resolver.run(self.resolver_job(root['mint']), self.scope)
        opener.assert_not_called()
        self.assertEqual(self.resolver.path.read_bytes(), before)
        self.assertEqual(self.reservations, [])

    def test_http_failure_retains_reservation_and_cursor(self):
        root = self.root()
        recover = self.recover(root, '{"cursor":27}')
        self.resolver.seed(self.scope)
        job = self.resolver_job(root['mint'])
        error = urllib.error.HTTPError('https://portal.sqd.dev', 529, 'busy', {'Retry-After':'60'}, None)
        with patch('economic_machine.pump_research.launch_resolver.portal_network', return_value=nullcontext()), \
                patch('economic_machine.pump_research.launch_resolver.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(urllib.error.HTTPError):
                self.resolver.run(job, self.scope)
        receipt = json.loads(self.resolver.path.read_bytes())['requests'][0]
        self.assertEqual(receipt['reserved_bytes'], RESPONSE_LIMIT+1)
        self.assertEqual(receipt['status'], 529)
        self.assertTrue(receipt['failed_reservation_retained'])
        self.assertEqual(receipt['retry_after'], '60')
        self.assertEqual(self.graph.db.execute('SELECT cursor FROM frontier WHERE id=?', (job['id'],)).fetchone()[0], job['cursor'])
        self.assertEqual(self.graph.db.execute('SELECT cursor FROM frontier WHERE id=?', (recover,)).fetchone()[0], '{"cursor":27}')
        self.assertIsNone(self.graph.state('launch_anchor:'+root['mint']))

    def test_future_block_and_oversized_response_keep_unvalidated_evidence(self):
        root = self.root()
        self.resolver.seed(self.scope)
        job = self.resolver_job(root['mint'])
        for raw in (b'{"block_number":456000001}', b'x'*(RESPONSE_LIMIT+1)):
            with self.assertRaises(ValueError):
                self.run_with_raw(job, raw)
        ledger = json.loads(self.resolver.path.read_bytes())
        self.assertEqual(len(ledger['requests']), 2)
        self.assertTrue(all(not row['validated'] for row in ledger['requests']))
        self.assertTrue(all((self.resolver.folder/row['raw_file']).exists() for row in ledger['requests']))
        self.assertIsNone(self.graph.state('launch_anchor:'+root['mint']))

    def test_original_deadline_cannot_be_extended(self):
        root = self.root()
        self.scope.grant['deadline_ms'] += 1
        with self.assertRaisesRegex(ValueError, 'LAUNCH_ORIGINAL_DEADLINE'):
            self.resolver.seed(self.scope)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
