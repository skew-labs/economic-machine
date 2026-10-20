import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from economic_machine.arbitrum_research import freezer_archive_storage as archive


class Spend:
    def __init__(self, digest, size):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE objects(digest TEXT,bytes INTEGER,verified INTEGER)')
        self.db.execute('INSERT INTO objects VALUES(?,?,1)', (digest, size))
        self.reservations = []
        self.published = 0

    def reserve(self, kind, attempt, **kwargs):
        self.reservations.append((kind, attempt, kwargs))

    def publish(self):
        self.published += 1


class ArchiveStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = b'verified original graph bytes' * 20
        self.packed = gzip.compress(self.raw, compresslevel=3, mtime=0)
        self.digest = hashlib.sha256(self.packed).hexdigest()
        self.proof = dict(chain_id=42161, encoding='gzip', complete_cloud_readback=True,
                          raw_bytes=len(self.raw), raw_sha256=hashlib.sha256(self.raw).hexdigest(),
                          cloud=dict(sha256=self.digest, bytes=len(self.packed)))
        self.storage = self.make_storage(self.root/'worker')
        self.addCleanup(self.storage.cloud.spend.db.close)
        self.clock = patch.object(archive.time, 'time_ns', return_value=1792200000000000000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def make_storage(self, base):
        storage = archive.ArchiveStorage.__new__(archive.ArchiveStorage)
        storage.base = base
        storage.chain_id = 42161
        storage.staging = base/'staging'
        storage.receipts = base/'evidence/objects'
        storage.staging.mkdir(parents=True)
        storage.receipts.mkdir(parents=True)
        storage.cloud = SimpleNamespace(binary=Path('/fake/blob_archive'), config=Path('/fake/config'),
            spend=Spend(self.digest, len(self.packed)), guard_local=Mock(),
            cached=Mock(side_effect=AssertionError('archive must not use global cache')))
        return storage

    def native(self, command, **kwargs):
        self.assertEqual(command[1], '--restore')
        self.assertEqual(command[3], self.digest)
        self.assertTrue(kwargs['capture_output'])
        self.assertLessEqual(kwargs['timeout'], 90)
        Path(command[4]).write_bytes(self.packed)
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(
            sha256=self.digest, bytes=len(self.packed), fullDownloadVerified=True)).encode())

    def receipts(self, storage=None):
        return [json.loads(p.read_text()) for p in (storage or self.storage).receipts.glob('*.archive-read.json')]

    def run_read(self, proof=None, native=None):
        with patch.object(archive.subprocess, 'run', side_effect=native or self.native):
            return self.storage.read(proof or self.proof)

    def assert_failed_retained(self):
        receipts = self.receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['state'], 'FAILED_RETAINED')
        self.assertTrue(receipts[0]['failed_duplicate_retained'])
        self.assertTrue(Path(receipts[0]['target']).is_file())
        self.assertEqual(len(self.storage.cloud.spend.reservations), 1)

    def test_success_full_checks_retire_only_own_verified_duplicate(self):
        sentinel = self.storage.staging/'previous-failed.gz'
        sentinel.write_bytes(b'preserve')
        self.assertEqual(self.run_read(), self.raw)
        self.assertEqual(sentinel.read_bytes(), b'preserve')
        receipt = self.receipts()[0]
        self.assertEqual(receipt['state'], 'VERIFIED_DUPLICATE_RETIRED')
        self.assertTrue(receipt['full_native_readback'])
        self.assertTrue(receipt['raw_hash_verified'])
        self.assertFalse(Path(receipt['target']).exists())
        self.storage.cloud.cached.assert_not_called()
        self.storage.cloud.guard_local.assert_called_once_with(len(self.packed)*2)
        self.assertEqual(self.storage.cloud.spend.db.execute('SELECT * FROM objects').fetchone(),
                         (self.digest, len(self.packed), 1))

    def test_each_fresh_read_reserved_and_audited_independently(self):
        self.run_read(); self.run_read()
        spend = self.storage.cloud.spend
        self.assertEqual(len(spend.reservations), 2)
        self.assertNotEqual(spend.reservations[0][1], spend.reservations[1][1])
        self.assertEqual(len(self.receipts()), 2)
        self.assertEqual(spend.published, 2)

    def test_failed_native_retains_partial_and_cost_reservation(self):
        def fail(command, **kwargs):
            Path(command[4]).write_bytes(b'partial')
            return SimpleNamespace(returncode=1, stdout=b'')
        with self.assertRaisesRegex(ValueError, 'NATIVE_FAILED'):
            self.run_read(native=fail)
        self.assert_failed_retained()

    def test_native_timeout_retains_partial_and_cost_reservation(self):
        def fail(command, **kwargs):
            Path(command[4]).write_bytes(b'partial')
            raise archive.subprocess.TimeoutExpired(command, kwargs['timeout'])
        with self.assertRaises(archive.subprocess.TimeoutExpired):
            self.run_read(native=fail)
        self.assert_failed_retained()

    def test_false_native_verification_rejected_and_retained(self):
        def invalid(command, **kwargs):
            result = self.native(command, **kwargs)
            body = json.loads(result.stdout); body['fullDownloadVerified'] = False
            result.stdout = json.dumps(body).encode()
            return result
        with self.assertRaisesRegex(ValueError, 'NATIVE_PROOF'):
            self.run_read(native=invalid)
        self.assert_failed_retained()

    def test_compressed_digest_is_independently_verified(self):
        def corrupt(command, **kwargs):
            result = self.native(command, **kwargs)
            Path(command[4]).write_bytes(b'x'*len(self.packed))
            return result
        with self.assertRaisesRegex(ValueError, 'COMPRESSED_HASH'):
            self.run_read(native=corrupt)
        self.assert_failed_retained()

    def test_raw_hash_is_independently_verified(self):
        proof = copy.deepcopy(self.proof); proof['raw_sha256'] = 'a'*64
        with self.assertRaisesRegex(ValueError, 'RAW_HASH'):
            self.run_read(proof)
        self.assert_failed_retained()

    def test_expansion_past_declared_raw_bound_rejected(self):
        proof = copy.deepcopy(self.proof); proof['raw_bytes'] = 1
        with self.assertRaisesRegex(ValueError, 'RAW_HASH'):
            self.run_read(proof)
        self.assert_failed_retained()

    def test_trailing_gzip_member_rejected(self):
        self.packed += gzip.compress(b'extra', mtime=0)
        self.digest = hashlib.sha256(self.packed).hexdigest()
        self.proof['cloud'] = dict(sha256=self.digest, bytes=len(self.packed))
        self.storage.cloud.spend.db.execute('DELETE FROM objects')
        self.storage.cloud.spend.db.execute('INSERT INTO objects VALUES(?,?,1)', (self.digest,len(self.packed)))
        with self.assertRaisesRegex(ValueError, 'RAW_HASH'):
            self.run_read()
        self.assert_failed_retained()

    def test_bounds_and_scope_fail_before_reservation(self):
        for key,value in [('raw_bytes', archive.MAX_OBJECT+1), ('raw_bytes',True),
                          ('chain_id',4663), ('complete_cloud_readback',False)]:
            proof=copy.deepcopy(self.proof);proof[key]=value
            with self.subTest(key=key,value=value), self.assertRaisesRegex(ValueError,'READ_PROOF'):
                self.run_read(proof)
        proof=copy.deepcopy(self.proof);proof['cloud']['bytes']=archive.MAX_OBJECT+1
        with self.assertRaisesRegex(ValueError,'READ_REFERENCE'):
            self.run_read(proof)
        self.assertEqual(self.storage.cloud.spend.reservations, [])

    def test_unverified_object_cannot_be_restored(self):
        self.storage.cloud.spend.db.execute('UPDATE objects SET verified=0')
        with self.assertRaisesRegex(ValueError,'UNVERIFIED_OBJECT'):
            self.run_read()
        self.assertEqual(self.storage.cloud.spend.reservations, [])

    def test_deadline_before_admission_does_not_reserve_or_send(self):
        with patch.object(archive.time,'time_ns',return_value=archive.DEADLINE*1000000):
            with self.assertRaisesRegex(ValueError,'DEADLINE'):
                self.run_read()
        self.assertEqual(self.storage.cloud.spend.reservations, [])

    def test_floor_and_budget_admission_do_not_send(self):
        self.storage.cloud.guard_local.side_effect=ValueError('FREE_FLOOR')
        with patch.object(archive.subprocess,'run') as native:
            with self.assertRaisesRegex(ValueError,'FREE_FLOOR'):
                self.storage.read(self.proof)
            native.assert_not_called()
        self.assertEqual(self.storage.cloud.spend.reservations, [])
        self.storage.cloud.guard_local.side_effect=None
        self.storage.cloud.spend.reserve=Mock(side_effect=ValueError('MONTHLY_BUDGET'))
        with patch.object(archive.subprocess,'run') as native:
            with self.assertRaisesRegex(ValueError,'MONTHLY_BUDGET'):
                self.storage.read(self.proof)
            native.assert_not_called()

    def test_native_path_symlink_is_rejected_without_deleting_target(self):
        external=self.root/'unrelated';external.write_bytes(self.packed)
        def symlink(command,**kwargs):
            result=self.native(command,**kwargs);target=Path(command[4]);target.unlink();target.symlink_to(external)
            return result
        with self.assertRaises(OSError):
            self.run_read(native=symlink)
        self.assertEqual(external.read_bytes(),self.packed)
        self.assertEqual(self.receipts()[0]['state'],'FAILED_RETAINED')

    def test_two_workers_overlap_native_reads_with_separate_staging(self):
        barrier=threading.Barrier(2)
        paths=[];lock=threading.Lock()
        def native(command,**kwargs):
            with lock:paths.append(command[4])
            barrier.wait(timeout=3)
            return self.native(command,**kwargs)
        def worker(number):
            storage=self.make_storage(self.root/str(number))
            try:
                result=storage.read(self.proof)
                return result,self.receipts(storage)
            finally:storage.cloud.spend.db.close()
        with patch.object(archive.subprocess,'run',side_effect=native), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(worker,range(2)))
        self.assertEqual([r[0] for r in outcomes],[self.raw,self.raw])
        self.assertEqual(len(set(paths)),2)
        self.assertNotEqual(Path(paths[0]).parent,Path(paths[1]).parent)
        self.assertTrue(all(r[1][0]['state']=='VERIFIED_DUPLICATE_RETIRED' for r in outcomes))

    def test_default_off_and_explicit_archive_only_selection(self):
        ordinary=Mock(return_value='ordinary')
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(archive.archive_storage('a','b',chain_id=42161,ordinary_storage=ordinary),'ordinary')
        with patch.dict(os.environ,{'EM_FREEZER_ARCHIVE_DIRECT_RESTORE':'1'}),patch.object(archive,'ArchiveStorage',return_value='direct'):
            self.assertEqual(archive.archive_storage('a','b',chain_id=42161,ordinary_storage=ordinary),'direct')
        with patch.dict(os.environ,{'EM_FREEZER_ARCHIVE_DIRECT_RESTORE':'yes'}):
            with self.assertRaisesRegex(ValueError,'SETTING'):
                archive.archive_storage('a','b',chain_id=42161)


if __name__=='__main__':unittest.main()
