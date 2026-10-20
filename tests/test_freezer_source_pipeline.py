from concurrent.futures import Future
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from economic_machine.arbitrum_research.freezer_source_pipeline import PipelinedSnapshot, UploadLane


class Gate:
    def __init__(self, folder, guard, **kwargs): self.guard = guard
    def __enter__(self): self.guard()
    def __exit__(self, *args): return False


class Uploads:
    def __init__(self): self.jobs = []
    def submit(self, path):
        f = Future()
        self.jobs.append((f, path))
        return f
    def finish(self, index, error=None, wrong=False):
        f, path = self.jobs[index]
        if error:
            f.set_exception(error)
        else:
            raw = path.read_bytes()
            f.set_result(dict(sha256='wrong' if wrong else hashlib.sha256(raw).hexdigest(), bytes=len(raw)))


class PipelinedSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s = self.s = PipelinedSnapshot.__new__(PipelinedSnapshot)
        s.out = Path(self.tmp.name)
        (s.out / 'ranges').mkdir()
        s.catalogue = s.out
        s.parts = [dict(offset=0, bytes=1024**2, url='https://example.test/pinned')]
        s.pins = {}
        s.db = sqlite3.connect(':memory:')
        s.db.executescript('CREATE TABLE attempts(id,bytes,state); CREATE TABLE ranges(id,start,bytes,sha256,proof); CREATE TABLE sources(url PRIMARY KEY,body);')
        s.guard = lambda n=0: None
        s.cloud = SimpleNamespace(spend=SimpleNamespace(reserve=lambda *a, **kw: None))
        s.http = SimpleNamespace(read=lambda part, relative, n, pin, **kw: (bytes([relative % 251]) * n, dict(etag='fixed')))
        s.uploads = Uploads()
        s.pending = {}
        s.last_refs = []
        s.calls = s.network_bytes = 0
        s.metrics = dict(provider_seconds=0, blob_wait_seconds=0, requests=0, max_pending=0, cache_reads=0,gate_wait_seconds=0,http_seconds=0)
        self.gate = patch('economic_machine.arbitrum_research.freezer_source_pipeline.ArchiveGate', Gate)
        self.gate.start()

    def tearDown(self):
        self.gate.stop()
        self.s.db.close()
        self.tmp.cleanup()

    def test_unverified_range_never_commits(self):
        self.s.read(8, 32)
        self.assertEqual(self.s.db.execute('select count(*) from ranges').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('select state from attempts').fetchone()[0], 'reserved')
        self.assertNotIn('cloud', self.s.last_refs[0])
        self.s.uploads.finish(0)
        self.s.drain()
        self.assertEqual(self.s.db.execute('select state from attempts').fetchone()[0], 'received')
        self.assertIn('cloud', self.s.last_refs[0])

    def test_blob_failure_preserves_reservation_and_raw(self):
        self.s.read(8, 32)
        self.s.uploads.finish(0, error=ValueError('readback failed'))
        with self.assertRaisesRegex(ValueError, 'readback failed'):
            self.s.drain()
        self.assertEqual(self.s.db.execute('select count(*) from ranges').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('select state from attempts').fetchone()[0], 'reserved')
        self.assertEqual(len(list((self.s.out / 'ranges').glob('*.raw'))), 1)

    def test_wrong_proof_fails_closed(self):
        self.s.read(8, 32)
        self.s.uploads.finish(0, wrong=True)
        with self.assertRaisesRegex(ValueError, 'BLOB_PROOF'):
            self.s.drain()
        self.assertEqual(self.s.db.execute('select count(*) from ranges').fetchone()[0], 0)

    def test_duplicate_pending_read_does_not_reserve_twice(self):
        a = self.s.read(8, 32)
        self.s.uploads.finish(0)
        self.assertEqual(self.s.read(8, 32), a)
        self.assertEqual(self.s.db.execute('select count(*) from attempts').fetchone()[0], 1)
        self.assertEqual(self.s.last_refs[0], self.s.last_refs[1])

    def test_two_pending_limit_commits_oldest_before_third(self):
        self.s.read(8, 32)
        self.s.read(40, 32)
        self.assertEqual(len(self.s.pending), 2)
        self.s.uploads.finish(0)
        self.s.read(72, 32)
        self.assertEqual(len(self.s.pending), 2)
        self.assertEqual(self.s.metrics['max_pending'], 2)
        self.assertEqual(self.s.db.execute('select count(*) from ranges').fetchone()[0], 1)

    def test_source_cap_prevents_dispatch(self):
        def guard(n=0):
            if n: raise ValueError('SOURCE_LIMIT')
        self.s.guard = guard
        with self.assertRaisesRegex(ValueError, 'SOURCE_LIMIT'):
            self.s.read(8, 32)
        self.assertEqual(self.s.db.execute('select count(*) from attempts').fetchone()[0], 0)
        self.assertEqual(self.s.uploads.jobs, [])

    def test_source_identity_pinned_before_upload_finishes(self):
        seen = []
        def read(part, relative, n, pin, **kwargs):
            seen.append(pin)
            return bytes(n), dict(etag='fixed')
        self.s.http.read = read
        self.s.read(8, 32)
        self.s.read(40, 32)
        self.assertEqual(seen, [None, dict(etag='fixed')])

    def test_larger_index_read_stays_inside_tar_member(self):
        s = self.s
        s.prefix = 'x'
        s.read_ahead = True
        raw = bytes(range(256)) * 4300
        s.files = {'x/headers.cidx': dict(bytes=len(raw), data_offset=512, directory=False)}
        calls = []
        def read(o, n):
            calls.append((o, n))
            return raw[o-512:o-512+n]
        s.read = read
        self.assertEqual(s.member('headers.cidx', 1048570, 50), raw[1048570:1048620])
        self.assertEqual(calls, [(512, 1048576), (512+1048576, len(raw)-1048576)])
        with self.assertRaisesRegex(ValueError, 'RANGE'):
            s.member('headers.cidx', len(raw)-10, 11)

    def test_lane_reuses_cloud_and_closes_on_own_thread(self):
        import threading
        events=[]
        class FakeCloud:
            def __init__(self,base):self.owner=threading.get_ident();events.append('created')
            def upload(self,path):
                if threading.get_ident()!=self.owner:raise ValueError('wrong owner')
                events.append('upload');return {'proof':str(path)}
            def close(self):
                if threading.get_ident()!=self.owner:raise ValueError('wrong close')
                events.append('closed')
        lane=UploadLane('/unused',factory=FakeCloud)
        try:
            for value in ['one','two']:self.assertEqual(lane.pool.submit(lane.upload,value).result(),{'proof':value})
        finally:lane.close()
        self.assertEqual(events,['created','upload','upload','closed'])


if __name__ == '__main__':
    unittest.main()
