"""Archive-only fresh Blob reads without the shared reader-cache network lock.

Each archive worker owns its Storage/Spend connection and staging directory.
Objects, byte bounds, native full readback and raw reconstruction are still
checked. Search readers retain the ordinary shared-cache implementation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid
import zlib

from .freezer_source import DEADLINE
from .storage import Storage
from ..pump_research.store import atomic

MAX_OBJECT = 64 * 1024**2


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


class ArchiveStorage(Storage):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        try:
            from .freezer_archive_guard import install_archive_guard
            install_archive_guard(self.cloud)
        except BaseException:
            self.cloud.close()
            raise

    def read(self, proof):
        if (type(proof) is not dict or proof.get('chain_id') != self.chain_id
                or proof.get('encoding') != 'gzip' or proof.get('complete_cloud_readback') is not True
                or type(proof.get('raw_bytes')) is not int or not 0 < proof['raw_bytes'] <= MAX_OBJECT
                or not _digest(proof.get('raw_sha256'))):
            raise ValueError('ARCHIVE_READ_PROOF')
        reference = proof.get('cloud')
        if (type(reference) is not dict or not _digest(reference.get('sha256'))
                or type(reference.get('bytes')) is not int or not 0 < reference['bytes'] <= MAX_OBJECT):
            raise ValueError('ARCHIVE_READ_REFERENCE')
        digest, size = reference['sha256'], reference['bytes']
        row = self.cloud.spend.db.execute('SELECT bytes,verified FROM objects WHERE digest=?', (digest,)).fetchone()
        if row != (size, 1):
            raise ValueError('ARCHIVE_READ_UNVERIFIED_OBJECT')
        if time.time_ns() // 1000000 >= DEADLINE:
            raise ValueError('ARCHIVE_READ_DEADLINE')
        # Retain the same two-copy headroom and authority checks as Cloud.cached.
        self.cloud.guard_local(size * 2)
        restore_root = self.staging / 'archive-restores'
        restore_root.mkdir(exist_ok=True)
        if restore_root.is_symlink():
            raise ValueError('ARCHIVE_READ_STAGING_TYPE')
        attempt = uuid.uuid4().hex
        target = restore_root / (attempt + '.gz')
        receipt = self.receipts / (attempt + '.archive-read.json')
        if target.exists() or target.is_symlink():
            raise ValueError('ARCHIVE_READ_TARGET_EXISTS')
        self.cloud.spend.reserve('read', 'archive-read:' + attempt, bytes_=size, digest=digest)
        record = dict(schema='economic-machine.freezer-archive-fresh-read.v1', state='RESERVED',
                      at_ms=time.time_ns() // 1000000, attempt=attempt, chain_id=self.chain_id,
                      cloud_sha256=digest, cloud_bytes=size, raw_sha256=proof['raw_sha256'],
                      raw_bytes=proof['raw_bytes'], target=str(target), original_blob_preserved=True)
        atomic(receipt, record)
        try:
            remaining = (DEADLINE - time.time_ns() // 1000000) / 1000
            if remaining <= 0:
                raise ValueError('ARCHIVE_READ_DEADLINE')
            run = subprocess.run([str(self.cloud.binary), '--restore', str(self.cloud.config),
                                  digest, str(target), str(size)], capture_output=True,
                                 timeout=min(90, remaining))
            if run.returncode:
                raise ValueError('ARCHIVE_READ_NATIVE_FAILED')
            if len(run.stdout) > 65536:
                raise ValueError('ARCHIVE_READ_NATIVE_PROOF_BOUND')
            native = json.loads(run.stdout)
            if (type(native) is not dict or native.get('sha256') != digest
                    or type(native.get('bytes')) is not int or native['bytes'] != size
                    or native.get('fullDownloadVerified') is not True):
                raise ValueError('ARCHIVE_READ_NATIVE_PROOF')
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
                    raise ValueError('ARCHIVE_READ_FILE_TYPE_OR_SIZE')
                packed = stream.read(size + 1)
                after = os.fstat(stream.fileno())
            if (len(packed) != size or hashlib.sha256(packed).hexdigest() != digest
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError('ARCHIVE_READ_COMPRESSED_HASH')
            decoder = zlib.decompressobj(31)
            raw = decoder.decompress(packed, proof['raw_bytes'] + 1)
            if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                    or len(raw) != proof['raw_bytes'] or hashlib.sha256(raw).hexdigest() != proof['raw_sha256']):
                raise ValueError('ARCHIVE_READ_RAW_HASH')
            record.update(state='VERIFIED', verified_at_ms=time.time_ns() // 1000000,
                          full_native_readback=True, compressed_hash_verified=True, raw_hash_verified=True)
            atomic(receipt, record)
            # Remove only this newly restored, fully verified duplicate. A failed
            # or interrupted native restore stays at its unique path for audit.
            current = target.lstat()
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('ARCHIVE_READ_FILE_CHANGED_BEFORE_RETIRE')
            target.unlink()
            record.update(state='VERIFIED_DUPLICATE_RETIRED', retired_at_ms=time.time_ns() // 1000000)
            atomic(receipt, record)
            self.cloud.spend.publish()
            return raw
        except BaseException as exc:
            record.update(state='FAILED_RETAINED', failed_at_ms=time.time_ns() // 1000000,
                          exception_class=type(exc).__name__, failed_duplicate_retained=target.exists())
            atomic(receipt, record)
            raise


def archive_storage(base, shared_base, *, chain_id, ordinary_storage=Storage):
    enabled = os.environ.get('EM_FREEZER_ARCHIVE_DIRECT_RESTORE', '0')
    if enabled not in ('0', '1'):
        raise ValueError('ARCHIVE_DIRECT_RESTORE_SETTING')
    return (ArchiveStorage if enabled == '1' else ordinary_storage)(base, shared_base, chain_id=chain_id)
