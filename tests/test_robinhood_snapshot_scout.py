import tarfile
import unittest
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from economic_machine.robinhood_research.snapshot_scout import candidates, MAX_READ, Scout, DEADLINE, validate_anchor


class ScoutTests(unittest.TestCase):
    def header(self, name='./l2chaindata/ancient/headers.cidx', size=12):
        h = tarfile.TarInfo(name); h.size = size
        return h.tobuf(format=tarfile.USTAR_FORMAT)

    def test_exact_header_and_absolute_offset(self):
        x = candidates(self.header(), 512, 8192)
        self.assertEqual(x[0]['header_offset'], 512)
        self.assertTrue(x[0]['history_candidate'])

    def test_body_magic_is_not_header(self):
        self.assertEqual(candidates(b'ustar'+bytes(507), 0, 8192), [])

    def test_invalid_checksum(self):
        h = bytearray(self.header()); h[0] ^= 1
        self.assertEqual(candidates(bytes(h), 0, 8192), [])

    def test_partial_header_is_not_candidate(self):
        self.assertEqual(candidates(self.header()[:511], 0, 8192), [])

    def test_unsafe_path(self):
        self.assertEqual(candidates(self.header('../escape'), 0, 8192), [])

    def test_member_past_end(self):
        self.assertEqual(candidates(self.header(size=8192), 0, 8192), [])

    def test_read_bound(self):
        for raw, start, total in [(b'',1,10), (bytes(1024),0,512), (bytes(MAX_READ+1),0,MAX_READ+1)]:
            with self.assertRaises(ValueError): candidates(raw,start,total)

    def test_zero_padding(self):
        self.assertEqual(candidates(bytes(4096), 0, 4096), [])

    def anchor(self):
        import hashlib
        raw=self.header();e=candidates(raw,0,8192)[0]
        return dict(start=0,headers=[dict(entry=e,raw_hex=raw.hex(),
            proof=dict(sha256=hashlib.sha256(raw).hexdigest(),bytes=512))],next_offset=e['next_offset'])

    def test_anchor_requires_full_blob_verification(self):
        a=self.anchor();self.assertEqual(validate_anchor(a,8192,lambda _: (512,1)),1024)
        for receipt in [(512,0),None,(511,1)]:
            with self.assertRaisesRegex(ValueError,'ANCHOR_BLOB'):validate_anchor(a,8192,lambda _:receipt)

    def test_anchor_cursor_and_link_must_match(self):
        a=self.anchor();a['start']=512
        with self.assertRaisesRegex(ValueError,'ANCHOR_GAP'):validate_anchor(a,8192,lambda _: (512,1))
        a=self.anchor();a['next_offset']=2048
        with self.assertRaisesRegex(ValueError,'ANCHOR_CURSOR'):validate_anchor(a,8192,lambda _: (512,1))

    def test_anchor_derived_header_cannot_override_raw(self):
        a=self.anchor();a['headers'][0]['entry']['bytes']=999
        with self.assertRaisesRegex(ValueError,'ANCHOR_HEADER'):validate_anchor(a,8192,lambda _: (512,1))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db = sqlite3.connect(self.root/'runtime.sqlite3')
        db.executescript('''CREATE TABLE identity(body TEXT PRIMARY KEY);
          CREATE TABLE cursor(id INTEGER PRIMARY KEY,offset INTEGER,finished INTEGER);
          CREATE TABLE attempts(id TEXT PRIMARY KEY,offset INTEGER,bytes INTEGER,state TEXT);
          CREATE TABLE sources(url TEXT PRIMARY KEY,body TEXT);''')
        db.execute('INSERT INTO identity VALUES(?)', (json.dumps(dict(deadline_ms=DEADLINE,parts=[dict(bytes=MAX_READ*4)])),))
        db.execute('INSERT INTO cursor VALUES(1,512,0)')
        db.commit(); db.close()
        self.cloud = Mock()

    def tearDown(self): self.tmp.cleanup()

    @patch('economic_machine.robinhood_research.snapshot_scout.time.time_ns', return_value=1_000_000)
    def test_reservation_keeps_original_cursor_and_ledger(self, _):
        s = Scout(self.root, self.root/'out', self.cloud)
        try:
            first = s.reserve(1024, 512); second = s.reserve(2048, 512)
            self.assertNotEqual(first, second)
            self.assertEqual(s.db.execute('SELECT offset,finished FROM cursor').fetchone(), (512,0))
            self.assertEqual(s.db.execute('SELECT count(*),sum(bytes) FROM attempts').fetchone(), (2,1024))
            self.assertEqual(self.cloud.spend.reserve.call_count, 2)
        finally: s.close()

    @patch('economic_machine.robinhood_research.snapshot_scout.time.time_ns', return_value=1_000_000)
    def test_shared_request_limit_preserved(self, _):
        s = Scout(self.root, self.root/'out', self.cloud)
        try:
            s.db.executemany('INSERT INTO attempts VALUES(?,?,?,?)',
                [(str(i),i*512,512,'reserved') for i in range(20000)])
            s.db.commit()
            with self.assertRaisesRegex(ValueError, 'CATALOGUE_REQUEST_BOUND'): s.reserve(0,512)
            self.assertEqual(s.db.execute('SELECT count(*) FROM attempts').fetchone()[0],20000)
            self.cloud.spend.reserve.assert_not_called()
        finally: s.close()

    @patch('economic_machine.robinhood_research.snapshot_scout.time.time_ns', return_value=(DEADLINE+1)*1_000_000)
    def test_deadline_rejects_before_reservation(self, _):
        s = Scout(self.root, self.root/'out', self.cloud)
        try:
            with self.assertRaisesRegex(ValueError, 'SCOUT_DEADLINE'): s.reserve(0,512)
            self.assertEqual(s.db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
        finally: s.close()

    @patch('economic_machine.robinhood_research.snapshot_scout.time.time_ns', return_value=1_000_000)
    def test_failed_budget_reservation_remains_reserved(self, _):
        self.cloud.spend.reserve.side_effect = ValueError('budget')
        s = Scout(self.root, self.root/'out', self.cloud)
        try:
            with self.assertRaisesRegex(ValueError, 'budget'): s.reserve(0,512)
            self.assertEqual(s.db.execute('SELECT bytes,state FROM attempts').fetchall(), [(512,'reserved')])
        finally: s.close()


if __name__ == '__main__': unittest.main()
