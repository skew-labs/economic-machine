"""One prepared-page Blob task overlapping native classification.

Each task owns its Storage/Cloud/Spend instance. A caller must finish this
task successfully before graph admission, including when native work finishes
first. This module does not fetch source ranges or register search shards.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import time

from .freezer_source import DEADLINE
from economic_machine.pump_research.store import atomic


def enabled(chain, prepared, setting, existing_policy=None):
    if setting not in ('0', '1'):
        raise ValueError('ENVELOPE_OVERLAP_SETTING')
    if existing_policy is not None:
        value = existing_policy.get('source_envelope_overlap', False)
        if type(value) is not bool:
            raise ValueError('ENVELOPE_OVERLAP_POLICY')
    else:
        value = setting == '1' and chain == 42161 and prepared
    if value and (chain != 42161 or prepared is not True):
        raise ValueError('ENVELOPE_OVERLAP_SCOPE')
    return value


class PreparedArchive:
    def __init__(self, out, shared_base, *, storage_factory=None, now_ms=None):
        self.out = Path(out) / 'source-envelope-overlap'
        self.out.mkdir(parents=True, exist_ok=True)
        self.shared_base = Path(shared_base)
        if storage_factory is None:
            from .freezer_archive_storage import ArchiveStorage
            storage_factory = ArchiveStorage
        self.storage_factory = storage_factory
        self.now_ms = now_ms or (lambda: time.time_ns() // 1000000)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='prepared-blob')
        self.future = None
        self.metrics = None
        self.closed = False

    def guard(self):
        if self.now_ms() >= DEADLINE:
            raise ValueError('ENVELOPE_OVERLAP_DEADLINE')

    def start(self, raw, source):
        self.guard()
        if self.closed or self.future is not None:
            raise ValueError('ENVELOPE_OVERLAP_ALREADY_STARTED')
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 60 * 1024**2:
            raise ValueError('ENVELOPE_OVERLAP_BOUND')
        self.future = self.executor.submit(self._archive, raw, source)

    def _archive(self, raw, source):
        storage = None
        started = time.monotonic()
        metrics = dict(started_at_ms=self.now_ms(), raw_bytes=len(raw),
                       raw_sha256=hashlib.sha256(raw).hexdigest(), workers=1,
                       source_requests=0, independent_storage_instance=True)
        try:
            self.guard()
            storage = self.storage_factory(self.out, self.shared_base, chain_id=42161)
            storage.cloud.guard_local(len(raw) * 2)
            before_put = time.monotonic()
            proof = storage.put(raw, 'snapshot_freezer_records', source)
            metrics['put_seconds'] = time.monotonic() - before_put
            self.guard()
            before_read = time.monotonic()
            restored = storage.read(proof)
            metrics['read_seconds'] = time.monotonic() - before_read
            if (proof.get('chain_id') != 42161 or proof.get('kind') != 'snapshot_freezer_records'
                    or proof.get('complete_cloud_readback') is not True
                    or proof.get('raw_bytes') != len(raw)
                    or proof.get('raw_sha256') != metrics['raw_sha256']
                    or proof.get('source') != source or restored != raw):
                raise ValueError('ENVELOPE_OVERLAP_READBACK')
            self.guard()
            storage.cloud.guard_local(0)
            metrics.update(state='VERIFIED_SOURCE_ENVELOPE', ended_at_ms=self.now_ms(),
                           worker_seconds=time.monotonic() - started)
            atomic(self.out / 'result.json', dict(metrics=metrics, proof=proof))
            self.metrics = metrics
            return proof
        except BaseException as error:
            metrics.update(state='FAILED_PRESERVED', ended_at_ms=self.now_ms(),
                           worker_seconds=time.monotonic() - started,
                           error_type=type(error).__name__)
            # Do not copy exception text: provider messages may contain URLs.
            atomic(self.out / ('failure-' + str(self.now_ms()) + '.json'), metrics)
            self.metrics = metrics
            raise
        finally:
            if storage is not None:
                storage.close()

    def finish(self):
        if self.future is None:
            raise ValueError('ENVELOPE_OVERLAP_NOT_STARTED')
        self.guard()
        started = time.monotonic()
        proof = self.future.result()
        self.guard()
        self.metrics['consumer_wait_seconds'] = time.monotonic() - started
        return proof

    def close(self):
        # Native failure cannot abandon in-flight preservation or erase receipts.
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.closed = True
