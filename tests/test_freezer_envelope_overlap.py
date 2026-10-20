from historical_scope import HistoricalScopeTestCase
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from economic_machine.arbitrum_research.freezer_envelope_overlap import PreparedArchive, enabled
from economic_machine.arbitrum_research.freezer_source import DEADLINE


class FakeStorage:
    def __init__(self, out, shared, *, chain_id, behavior=None):
        self.out = out
        self.chain_id = chain_id
        self.behavior = behavior or {}
        self.thread = threading.get_ident()
        self.closed = False
        self.cloud = self
        self.events = []
        self.proof = None

    def guard_local(self, extra):
        self.events.append(('guard', extra))
        if self.behavior.get('floor_failure'):
            raise ValueError('RESEARCH_CLOUD_LOCAL_FREE_SPACE')

    def put(self, raw, kind, source):
        self.events.append(('put', len(raw)))
        (self.out / 'retained-attempt.raw').write_bytes(raw)
        if self.behavior.get('started'):
            self.behavior['started'].set()
        if self.behavior.get('release'):
            assert self.behavior['release'].wait(2)
        if self.behavior.get('put_error'):
            raise ValueError('BUDGET_BLOCKED_PRIVATE_URL_MUST_NOT_APPEAR')
        self.raw = raw
        self.proof = dict(chain_id=42161, kind=kind, source=source, raw_bytes=len(raw),
                          raw_sha256=hashlib.sha256(raw).hexdigest(), complete_cloud_readback=True)
        if self.behavior.get('after_put'):
            self.behavior['after_put']()
        return self.proof

    def read(self, proof):
        self.events.append(('read', len(self.raw)))
        if self.behavior.get('after_read'):
            self.behavior['after_read']()
        if self.behavior.get('tamper_proof'):
            proof['raw_sha256'] = '0' * 64
        return b'bad' if self.behavior.get('read_mismatch') else self.raw

    def close(self):
        self.closed = True


class OverlapTests(HistoricalScopeTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workers = []
        self.tasks = []

    def tearDown(self):
        for task in self.tasks:
            task.close()
        self.temp.cleanup()

    def task(self, behavior=None, now_ms=None):
        def factory(*args, **kwargs):
            storage = FakeStorage(*args, **kwargs, behavior=behavior)
            self.workers.append(storage)
            return storage
        task = PreparedArchive(self.root, self.root, storage_factory=factory, now_ms=now_ms)
        self.tasks.append(task)
        return task

    def test_success_created_and_closed_on_worker_thread(self):
        task = self.task()
        task.start(b'public source', {'identity': 'pinned'})
        proof = task.finish()
        self.assertEqual(proof['raw_sha256'], hashlib.sha256(b'public source').hexdigest())
        self.assertNotEqual(self.workers[0].thread, threading.get_ident())
        self.assertTrue(self.workers[0].closed)
        self.assertEqual(task.metrics['state'], 'VERIFIED_SOURCE_ENVELOPE')
        self.assertEqual([x[0] for x in self.workers[0].events], ['guard', 'put', 'read', 'guard'])

    def test_work_really_overlaps_caller_and_finish_waits(self):
        started, release = threading.Event(), threading.Event()
        task = self.task({'started': started, 'release': release})
        task.start(b'raw', {})
        self.assertTrue(started.wait(1))
        self.assertFalse(task.future.done())
        release.set()
        self.assertTrue(task.finish()['complete_cloud_readback'])

    def test_native_failure_close_drains_without_erasing_preservation(self):
        started, release = threading.Event(), threading.Event()
        task = self.task({'started': started, 'release': release})
        task.start(b'retained source', {})
        self.assertTrue(started.wait(1))
        done = threading.Event()
        thread = threading.Thread(target=lambda: (task.close(), done.set()))
        thread.start()
        self.assertFalse(done.wait(.02))
        release.set(); thread.join(1)
        self.assertTrue(done.is_set())
        self.assertEqual((task.out / 'retained-attempt.raw').read_bytes(), b'retained source')
        self.assertTrue((task.out / 'result.json').exists())

    def test_budget_failure_preserves_attempt_and_does_not_read(self):
        task = self.task({'put_error': True})
        task.start(b'raw', {})
        with self.assertRaisesRegex(ValueError, 'BUDGET_BLOCKED'):
            task.finish()
        self.assertNotIn('read', [x[0] for x in self.workers[0].events])
        self.assertTrue((task.out / 'retained-attempt.raw').exists())
        failure = next(task.out.glob('failure-*.json')).read_text()
        self.assertNotIn('PRIVATE_URL', failure)

    def test_free_floor_failure_precedes_put(self):
        task = self.task({'floor_failure': True})
        task.start(b'raw', {})
        with self.assertRaisesRegex(ValueError, 'LOCAL_FREE_SPACE'):
            task.finish()
        self.assertEqual([x[0] for x in self.workers[0].events], ['guard'])

    def test_corrupt_restoration_rejects(self):
        task = self.task({'read_mismatch': True})
        task.start(b'raw', {})
        with self.assertRaisesRegex(ValueError, 'READBACK'):
            task.finish()
        self.assertFalse((task.out / 'result.json').exists())

    def test_corrupt_proof_rejects(self):
        task = self.task({'tamper_proof': True})
        task.start(b'raw', {})
        with self.assertRaisesRegex(ValueError, 'READBACK'):
            task.finish()

    def test_deadline_before_start(self):
        task = self.task(now_ms=lambda: DEADLINE)
        with self.assertRaisesRegex(ValueError, 'DEADLINE'):
            task.start(b'raw', {})
        self.assertEqual(self.workers, [])

    def test_deadline_after_put_prevents_read(self):
        clock = [DEADLINE - 1]
        task = self.task({'after_put': lambda: clock.__setitem__(0, DEADLINE)}, now_ms=lambda: clock[0])
        task.start(b'raw', {})
        with self.assertRaisesRegex(ValueError, 'DEADLINE'):
            task.finish()
        self.assertNotIn('read', [x[0] for x in self.workers[0].events])

    def test_deadline_after_read_prevents_success(self):
        clock = [DEADLINE - 1]
        task = self.task({'after_read': lambda: clock.__setitem__(0, DEADLINE)}, now_ms=lambda: clock[0])
        task.start(b'raw', {})
        with self.assertRaisesRegex(ValueError, 'DEADLINE'):
            task.finish()
        self.assertFalse((task.out / 'result.json').exists())

    def test_one_task_only_and_invalid_input(self):
        task = self.task()
        with self.assertRaisesRegex(ValueError, 'BOUND'):
            task.start(b'', {})
        with self.assertRaisesRegex(ValueError, 'NOT_STARTED'):
            task.finish()
        task.start(b'raw', {}); task.finish()
        with self.assertRaisesRegex(ValueError, 'ALREADY_STARTED'):
            task.start(b'new', {})

    def test_opt_in_scope_and_legacy_resume_policy(self):
        self.assertTrue(enabled(42161, True, '1'))
        self.assertFalse(enabled(42161, False, '1'))
        self.assertFalse(enabled(4663, True, '1'))
        self.assertFalse(enabled(42161, True, '1', {}))
        self.assertTrue(enabled(42161, True, '0', {'source_envelope_overlap': True}))
        with self.assertRaisesRegex(ValueError, 'SCOPE'):
            enabled(4663, True, '1', {'source_envelope_overlap': True})
        with self.assertRaisesRegex(ValueError, 'SETTING'):
            enabled(42161, True, 'yes')
        with self.assertRaisesRegex(ValueError, 'POLICY'):
            enabled(42161, True, '1', {'source_envelope_overlap': 1})


if __name__ == '__main__':
    unittest.main()
