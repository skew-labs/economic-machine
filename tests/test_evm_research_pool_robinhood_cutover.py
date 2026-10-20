"""Run against the Robinhood service checkout, not only the Arbitrum checkout."""
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from economic_machine.arbitrum_research.runtime import Runtime as BaseRuntime
from economic_machine.robinhood_research.runtime import Runtime


def measured(*args):
    return dict(observed_ms=time.time_ns() // 1000000, logical_cpus=16, cpu_busy_pct=30,
        io_pressure_pct=5, mem_available_bytes=32 * 1024**3, mem_total_bytes=48 * 1024**3,
        free_disk_bytes=256 * 1024**3, protected_free_bytes=128 * 1024**3,
        arb_current_bytes=0, monthly_remaining_krw=700000, source_backoff_seconds=0, backlog_blocks=100)


class RobinhoodCutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_robinhood_inherited_investigation_never_admits_model(self):
        runtime = Runtime.__new__(Runtime)
        runtime.fleet = Mock()
        # Deliberately no state/graph/bridge: a bypass must occur before all
        # legacy batch generation and before accessing any provider credential.
        with patch.dict(os.environ, {'EM_RESEARCH_POOL_ROOT': '/retained/pool'}):
            runtime.investigate({})
        runtime.fleet.investigate.assert_not_called()
        runtime.fleet.manage.assert_not_called()

    def test_arbitrum_base_manager_callsite_also_bypassed(self):
        runtime = BaseRuntime.__new__(BaseRuntime)
        runtime.base = self.base; runtime.out = self.base
        runtime.storage = Mock(); runtime.fleet = Mock(); runtime.resource_apply = False
        runtime.state = dict(last_manager_ms=0, head=100, next_block=0, backoff_until=0)
        runtime.save = Mock()
        with patch.dict(os.environ, {'EM_RESEARCH_POOL_ROOT': '/retained/pool'}), \
             patch('economic_machine.arbitrum_research.runtime.metrics', measured):
            runtime.manage(force=True)
        runtime.fleet.manage.assert_not_called()
        self.assertFalse(runtime.state['policy']['manager_response_applied'])
        self.assertEqual(runtime.state['policy']['effective_download_concurrency'], 1)

    def test_robinhood_manager_stays_native(self):
        runtime = Runtime.__new__(Runtime)
        runtime.state = dict(last_manager_ms=0)
        runtime.fleet = Mock(); runtime.reclamp = Mock()
        with patch.dict(os.environ, {'EM_RESEARCH_POOL_ROOT': '/retained/pool'}):
            runtime.manage(force=True)
        runtime.reclamp.assert_called_once()
        runtime.fleet.manage.assert_not_called()

    def test_actual_robinhood_publisher_reports_shared_pool(self):
        runtime = Runtime.__new__(Runtime)
        runtime.out = self.base; runtime.storage = Mock()
        runtime.storage.cloud.spend.totals.return_value = {'remaining_krw': 700000}
        runtime.graph = None; runtime.grant_hash = 'a' * 64; runtime.parallel_plan = None
        runtime.state = dict(next_block=0, head=100); runtime.db = sqlite3.connect(':memory:')
        runtime.db.executescript('CREATE TABLE pages(start INTEGER,end INTEGER,state TEXT);CREATE TABLE shards(state TEXT);')
        try:
            with patch.dict(os.environ, {'EM_RESEARCH_POOL_ROOT': '/retained/pool'}):
                status = runtime.publish('RUNNING')
        finally:
            runtime.db.close()
        self.assertEqual(status['configured_investigators'], 0)
        self.assertEqual(status['configured_resource_managers'], 0)
        self.assertEqual(status['shared_research_lane_limit'], 50)
        self.assertEqual(status['research_dispatch'], 'external_shared_50_lane_pool')
        self.assertFalse(status['whole_history_complete'])

    def test_inline_compatibility_when_no_external_pool(self):
        runtime = Runtime.__new__(Runtime)
        runtime.state = {'last_investigation_ms': time.time_ns() // 1000000}
        runtime.fleet = Mock()
        with patch.dict(os.environ, {'EM_RESEARCH_POOL_ROOT': ''}):
            runtime.investigate({})
        runtime.fleet.investigate.assert_not_called()  # retained ten-minute cadence


if __name__ == '__main__': unittest.main()
