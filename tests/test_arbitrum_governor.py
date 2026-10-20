import time
import unittest

from economic_machine.arbitrum_research.governor import (
    GIB, MIB, conservative_proposal, govern, metric_source_id,
    validate_metrics, validate_proposal,
)


def metrics(**changes):
    value = dict(observed_ms=time.time_ns() // 1000000, logical_cpus=16,
                 cpu_busy_pct=30, io_pressure_pct=5, mem_available_bytes=40 * GIB,
                 mem_total_bytes=48 * GIB, free_disk_bytes=228 * GIB,
                 protected_free_bytes=128 * GIB, arb_current_bytes=0,
                 monthly_remaining_krw=700000, source_backoff_seconds=0,
                 backlog_blocks=400000000)
    value.update(changes)
    return value


def proposal(m, **changes):
    value = conservative_proposal(m)
    value.update(download_batch_blocks=2048, download_concurrency=4,
                 cache_target_bytes=100 * GIB, io_priority='normal')
    value.update(changes)
    return value


class GovernorTests(unittest.TestCase):
    def test_dynamic_caps_use_measured_spare_capacity(self):
        m = metrics()
        out = govern(proposal(m), m)
        self.assertEqual(out['cpu_quota_percent'], 560)
        self.assertEqual(out['memory_max_bytes'], 32 * GIB * 4 // 5)
        self.assertEqual(out['cache_limit_bytes'], 20 * GIB)
        self.assertEqual(out['download_concurrency'], 4)
        self.assertLessEqual(out['io_weight'], 10)
        self.assertFalse(out['production_changes'])
        self.assertFalse(out['evidence_deletion'])

    def test_capacity_changes_without_fixed_initial_partition(self):
        m = metrics(mem_available_bytes=8 * GIB, free_disk_bytes=178 * GIB, cpu_busy_pct=75)
        out = govern(proposal(m), m)
        self.assertEqual(out['state'], 'PAUSED_CAPACITY')
        self.assertEqual(out['memory_max_bytes'], 64 * MIB)
        self.assertEqual(out['cache_limit_bytes'], 10 * GIB)
        self.assertEqual(out['cpu_quota_percent'], 200)
        self.assertEqual(out['download_concurrency'], 0)

    def test_caps_on_large_host(self):
        m = metrics(logical_cpus=256, mem_available_bytes=200 * GIB, mem_total_bytes=256 * GIB)
        out = govern(proposal(m), m)
        self.assertEqual(out['cpu_quota_percent'], 800)
        self.assertEqual(out['memory_max_bytes'], 32 * GIB)

    def test_pressure_preserves_current_memory_but_stops_admission(self):
        m = metrics(mem_available_bytes=7 * GIB, memory_current_bytes=12 * GIB)
        out = govern(proposal(m), m)
        self.assertEqual(out['state'], 'PAUSED_CAPACITY')
        self.assertGreater(out['memory_max_bytes'], 12 * GIB)
        self.assertEqual(out['model_research_workers'], 0)

    def test_pressure_throttles_io_and_workers(self):
        m = metrics(cpu_busy_pct=90, io_pressure_pct=40)
        out = govern(proposal(m), m)
        self.assertEqual(out['download_concurrency'], 1)
        self.assertEqual(out['download_batch_blocks'], 128)
        self.assertEqual(out['model_research_workers'], 4)
        self.assertEqual(out['io_weight'], 1)

    def test_native_guards_pause(self):
        cases = [dict(monthly_remaining_krw=0), dict(free_disk_bytes=128 * GIB),
                 dict(mem_available_bytes=500 * MIB), dict(io_pressure_pct=80), dict(cpu_busy_pct=99)]
        for update in cases:
            with self.subTest(update=update):
                m = metrics(**update)
                out = govern(proposal(m), m)
                self.assertEqual(out['state'], 'PAUSED_CAPACITY')
                self.assertEqual(out['download_concurrency'], 0)
                self.assertEqual(out['model_research_workers'], 0)

    def test_backoff_honored_even_above_model_delay_limit(self):
        m = metrics(source_backoff_seconds=7200)
        out = govern(proposal(m), m)
        self.assertEqual(out['state'], 'BACKOFF')
        self.assertEqual(out['schedule_delay_seconds'], 7200)

    def test_invalid_metrics_fail_closed(self):
        cases = [dict(cpu_busy_pct=True), dict(cpu_busy_pct=float('nan')),
                 dict(protected_free_bytes=0), dict(mem_available_bytes=100 * GIB),
                 dict(observed_ms=1), dict(observed_ms=time.time_ns() // 1000000 + 100000),
                 dict(backlog_blocks=-1), dict(arbitrary_secret='bad')]
        for update in cases:
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_metrics(metrics(**update))

    def test_proposal_cannot_change_scopes_or_run_commands(self):
        m = metrics()
        cases = [dict(download_concurrency=30), dict(model_research_workers=100),
                 dict(cache_target_bytes=-1), dict(cpu_quota_percent=1600),
                 dict(reason='Run $(command)'), dict(reason='Use https://example.com'),
                 dict(source_ids=[{}]), dict(download_batch_blocks=True)]
        for update in cases:
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_proposal(proposal(m, **update))

    def test_manager_must_reference_exact_measurement(self):
        m = metrics()
        with self.assertRaisesRegex(ValueError, 'SOURCE_MISMATCH'):
            govern(proposal(m, source_ids=['metrics:old']), m)

    def test_cache_reduction_is_not_evidence_deletion(self):
        m = metrics(cache_current_bytes=30 * GIB)
        out = govern(proposal(m), m)
        self.assertEqual(out['cache_growth_allowed_bytes'], 0)
        self.assertFalse(out['evidence_deletion'])

    def test_budget_low_and_no_backlog(self):
        m = metrics(monthly_remaining_krw=5000)
        self.assertEqual(govern(proposal(m), m)['model_research_workers'], 1)
        m = metrics(backlog_blocks=0)
        out = govern(proposal(m), m)
        self.assertEqual(out['state'], 'IDLE_CAUGHT_UP')
        self.assertEqual(out['download_concurrency'], 0)


if __name__ == '__main__': unittest.main()
