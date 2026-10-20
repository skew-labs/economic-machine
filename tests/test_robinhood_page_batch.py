import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from economic_machine.arbitrum_research.source import SourceIntegrityError, SourceLimit
from economic_machine.pump_research.store import canonical
from economic_machine.robinhood_research.source import PublicSource, parse_page, PAGE_V1, PAGE_V2
from economic_machine.robinhood_research.runtime import Runtime
from test_arbitrum_runtime import measured, FakeCloud, FakeFleet
from test_robinhood_hash_fetch import Network
from test_robinhood_research import envelope


def fetch(start=1, end=64, cap=64, network=None):
    source = PublicSource(hash_bound=True, page_blocks=cap)
    source.identity = envelope('eth_chainId', [], '0x1237')
    network = network or Network()
    with patch.object(source.rpc, 'exchange', side_effect=network.exchange):
        return source.fetch(start, end), network


class PageTests(unittest.TestCase):
    def test_large_page_matches_four_old_pages_with_same_request_limits(self):
        large, net = fetch()
        old = [fetch(n, n+15, 16)[0] for n in range(1, 65, 16)]
        self.assertEqual(large['blocks'], [b for p in old for b in p['blocks']])
        self.assertEqual(json.loads(large['raw'])['schema'], PAGE_V2)
        self.assertTrue(all(json.loads(p['raw'])['schema'] == PAGE_V1 for p in old))
        self.assertEqual(sum(map(len, net.calls)), 192)
        self.assertTrue(all(len(batch) <= 16 for batch in net.calls))
        self.assertEqual(parse_page(large['raw'], 1, 64)['blocks'], large['blocks'])

    def test_source_and_parser_enforce_page_bounds(self):
        page, _ = fetch(end=1000)
        self.assertEqual((page['last_block'], page['requested_end']), (64, 1000))
        bundle = json.loads(page['raw']); bundle['entries'].append(copy.deepcopy(bundle['entries'][-1]))
        with self.assertRaisesRegex(SourceIntegrityError, 'PAGE_RANGE'):
            parse_page(canonical(bundle).encode(), 1, 1000)
        bundle['entries'].pop(); bundle['schema'] = PAGE_V1
        with self.assertRaisesRegex(SourceIntegrityError, 'PAGE_RANGE'):
            parse_page(canonical(bundle).encode(), 1, 1000)

    def test_new_version_requires_hash_bound_records(self):
        page, _ = fetch(end=1)
        bundle = json.loads(page['raw']); bundle['entries'][0].pop('binding')
        with self.assertRaisesRegex(SourceIntegrityError, 'PAGE_HASH_BINDING_REQUIRED'):
            parse_page(canonical(bundle).encode(), 1, 1)

    def test_new_version_rejects_broken_parent_after_old_page_boundary(self):
        page, _ = fetch()
        bundle = json.loads(page['raw'])
        bundle['entries'][16]['block']['response']['result']['parentHash'] = '0x'+'ff'*32
        with self.assertRaisesRegex(SourceIntegrityError, 'PAGE_PARENT'):
            parse_page(canonical(bundle).encode(), 1, 64)

    def test_backoff_keeps_verified_prefix_and_retry_after(self):
        network = Network(); network.fail_at_batch = 9
        page, _ = fetch(network=network)
        self.assertEqual((page['last_block'], page['requested_end'], page['retry_after_seconds']), (32, 64, 180))
        self.assertEqual(len(parse_page(page['raw'], 1, 64)['blocks']), 32)

    def test_capacity_guard_keeps_prefix_within_raw_byte_bound(self):
        sample, _ = fetch(end=8)
        limit = len(sample['raw'])+512
        with patch('economic_machine.robinhood_research.source.LIMIT', limit):
            page, _ = fetch()
        self.assertEqual(page['last_block'], 8)
        self.assertLessEqual(len(page['raw']), limit)

    def test_first_group_exceeding_capacity_raises_for_runtime_reclamp(self):
        with patch('economic_machine.robinhood_research.source.LIMIT', 100):
            with self.assertRaises(SourceLimit):fetch()

    def test_trace_error_is_preserved_across_large_page(self):
        network = Network(); network.trace_error = True
        page, _ = fetch(network=network)
        self.assertEqual(len(page['blocks']), 64)
        self.assertTrue(all(b['trace_boundary'] == 'TRACE_UNAVAILABLE' for b in page['blocks']))
        self.assertEqual(json.loads(page['raw'])['entries'][0]['traces']['response']['error']['code'], -32601)

    def test_exact_short_tail(self):
        page, _ = fetch(1, 19)
        self.assertEqual((len(page['blocks']), page['last_block']), (19, 19))

    def test_invalid_modes_and_configs_are_rejected(self):
        for cap in (0, 15, 65, True, '64'):
            with self.assertRaises(ValueError):PublicSource(hash_bound=True, page_blocks=cap)
        with self.assertRaises(ValueError):PublicSource(hash_bound=False, page_blocks=64)

    def test_governor_preserves_source_reduction(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = SimpleNamespace(base=Path(tmp), out=Path(tmp),
                storage=SimpleNamespace(cloud=SimpleNamespace(spend=None)),
                source=SimpleNamespace(page_blocks=64), resource_reapply_pending=False,
                resource_apply=False, save=lambda:None,
                state=dict(head=10000, next_block=1, backoff_until=0,
                    applied_policy=dict(cpu_quota_percent=560, memory_max_bytes=24*1024**3, io_weight=10)))
            with patch('economic_machine.robinhood_research.runtime.metrics', lambda *a, **k:measured()), patch('economic_machine.robinhood_research.runtime.apply_limits', return_value={}):
                Runtime.reclamp(stub)
                self.assertEqual(stub.state['policy']['download_batch_blocks'], 64)
                stub.state['source_batch_cap'] = 8
                Runtime.reclamp(stub)
                self.assertEqual(stub.state['policy']['download_batch_blocks'], 8)
                self.assertEqual(stub.state['policy']['effective_download_concurrency'], 1)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name)
        (self.base/'evidence').mkdir(); FakeCloud.blobs = {}
        def make_source():
            s = PublicSource(hash_bound=True, page_blocks=64)
            s.identity = envelope('eth_chainId', [], '0x1237')
            s.head = lambda:1000
            s.rpc.exchange = Network().exchange
            return s
        self.patches = [
            patch('economic_machine.robinhood_research.authority.authority', return_value=({}, 'a'*64)),
            patch('economic_machine.robinhood_research.source.PublicSource', make_source),
            patch('economic_machine.arbitrum_research.muse.MuseFleet', lambda *a, **k:FakeFleet()),
            patch('economic_machine.arbitrum_research.storage.Cloud', FakeCloud),
            patch('economic_machine.robinhood_research.runtime.metrics', lambda *a, **k:measured()),
            patch('economic_machine.arbitrum_research.runtime.shutil.disk_usage', return_value=shutil._ntuple_diskusage(500*1024**3,100*1024**3,400*1024**3)),
        ]
        for p in self.patches:p.start()
        self.runtime = Runtime(self.base, self.base/'shared', self.base/'unused')

    def tearDown(self):
        self.runtime.close()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def test_restart_replays_archived_v2_without_refetching_or_double_counting(self):
        r = self.runtime; page = r.one()
        self.assertEqual(page['last_block'], 63)
        counts = r.graph.snapshot()['counts']
        # Recreate a crash after graph commit and before the cursor commit.
        with r.db:r.db.execute("UPDATE pages SET state='archived'")
        r.state['next_block'] = 0; r.save(); r.close()
        self.runtime = r = Runtime(self.base, self.base/'shared', self.base/'unused')
        with patch.object(r.source, 'fetch', side_effect=AssertionError('unexpected refetch')):
            replay = r.one()
        self.assertEqual(replay['sha256'], page['sha256'])
        self.assertEqual(r.state['next_block'], 64)
        self.assertEqual(r.graph.snapshot()['counts'], counts)
        self.assertEqual(r.db.execute("SELECT count(*) FROM pages WHERE state='indexed'").fetchone()[0], 1)

    def test_v1_journal_continues_with_v2_after_restart(self):
        r = self.runtime; r.source.page_blocks = 16
        first = r.one(); r.close()
        self.runtime = r = Runtime(self.base, self.base/'shared', self.base/'unused')
        second = r.one()
        self.assertEqual((first['last_block'], second['first_block'], second['last_block']), (15,16,79))
        self.assertEqual(r.state['next_block'], 80)
        self.assertEqual(r.graph.snapshot()['counts']['blocks'], 80)
        self.assertEqual(r.db.execute("SELECT count(*) FROM pages WHERE state='indexed'").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
