"""One pinned HTTP reader overlapping two fully verified Blob uploads.

Only this experimental snapshot subclass changes read scheduling. The original
ledger, reservations, provider lock, immutable range identity and caps remain.
Every prepared page drains its uploads before the producer can seal it.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
import uuid

from .freezer_source import Snapshot, segments, DEADLINE
from .freezer_authority import source_authority
from .freezer_source_guard import install_source_guard, source_inventory_metrics
from .freezer_range_store import PortableRanges, enabled
from economic_machine.pump_research.archive_gate import ArchiveGate
from economic_machine.pump_research.research_cloud import Cloud
from economic_machine.pump_research.store import atomic, canonical


class UploadLane:
    def __init__(self, base, factory=Cloud):
        self.base,self.factory,self.cloud=base,factory,None
        self.pool=ThreadPoolExecutor(max_workers=1)
        self.seconds=[]

    def upload(self, path):
        began=time.monotonic()
        if self.cloud is None:
            self.cloud=self.factory(self.base)
            try:install_source_guard(self.cloud)
            except BaseException:
                self.cloud.close();self.cloud=None;raise
        result=self.cloud.upload(path)
        self.seconds.append(time.monotonic()-began)
        return result

    def close_cloud(self):
        if self.cloud is not None:self.cloud.close()

    def close(self):
        try:self.pool.submit(self.close_cloud).result()
        finally:self.pool.shutdown(wait=True,cancel_futures=True)


class UploadPool:
    def __init__(self, base):
        self.lanes=[UploadLane(base),UploadLane(base)];self.turn=0

    def submit(self, path):
        lane=self.lanes[self.turn%2];self.turn+=1
        return lane.pool.submit(lane.upload,path)

    def close(self):
        try:self.lanes[0].close()
        finally:self.lanes[1].close()


def pending_range_limit(value):
    if type(value) is not str or value not in ('2','4'):raise ValueError('FREEZER_PENDING_RANGE_SETTING')
    return int(value)


class PipelinedSnapshot(Snapshot):
    max_pending_ranges=2
    def __init__(self, *args, source_authorization=None, **kwargs):
        self.source_authorization=source_authorization
        super().__init__(*args, **kwargs)
        if source_authorization is not None:
            source_authority(source_authorization,self.out,self.db.execute('select body from identity').fetchone()[0])
        install_source_guard(self.cloud)
        self.cloud_first=enabled()
        present=self.db.execute("select 1 from sqlite_master where type='table' and name='portable_ranges'").fetchone()
        self.portable=PortableRanges(self.out,self.db,self.cloud) if self.cloud_first or present else None
        self.uploads = UploadPool(self.cloud.base)
        self.pending = {}
        self.max_pending_ranges=pending_range_limit(os.environ.get('EM_FREEZER_SOURCE_PENDING_RANGES','2'))
        self.metrics = dict(started_at_ms=time.time_ns() // 1000000, provider_seconds=0,
                            blob_wait_seconds=0, requests=0, max_pending=0, pending_range_limit=self.max_pending_ranges,
                            cache_reads=0, index_read_ahead_bytes=1024**2,
                            guard_seconds=0,gate_wait_seconds=0,http_seconds=0)
        from .freezer_http_timing import instrument
        self.metrics['http_transport'] = instrument(self.http)

    def guard(self,n=0):
        began=time.monotonic()
        try:
            if self.source_authorization is None:
                return super().guard(n)
            if time.time_ns()//1000000>=DEADLINE:raise ValueError('FREEZER_DEADLINE')
            # The owner removed only the byte ceiling. The original deadline,
            # shared cost admission, host/local storage floors still apply.
            self.cloud.guard_local(max(n*2,1024**2))
        finally:
            if hasattr(self,'metrics'):self.metrics['guard_seconds']+=time.monotonic()-began

    def _finish(self, key):
        item = self.pending[key]
        began = time.monotonic()
        proof = item['future'].result()  # full Blob upload/readback is mandatory
        self.metrics['blob_wait_seconds'] += time.monotonic() - began
        if proof != dict(sha256=item['digest'], bytes=item['size']):
            raise ValueError('FREEZER_PIPELINED_BLOB_PROOF')
        disposable=item.get('working_path') is not None
        if disposable:
            record=self.portable.prepare(key,item,proof)
            self.portable.commit(key,item,proof,record)
        else:
            with self.db:
                self.db.execute('insert into ranges values(?,?,?,?,?)',
                    (key, item['absolute'], item['size'], item['digest'], canonical(proof)))
                self.db.executemany("update attempts set state='received' where id=?",
                                    [(i,) for i in item['ids']])
        item['ref']['cloud'] = proof
        del self.pending[key]
        if disposable:self.portable.retire_new(key,item,proof)

    def drain(self):
        while self.pending:
            self._finish(next(iter(self.pending)))

    def read(self, start, size):
        self.guard()
        if size < 0 or size > 32 * 1024**2:
            raise ValueError('FREEZER_READ_BOUND')
        if not size:
            return b''
        output = []
        for offset in range(start, start + size, 8 * 1024**2):
            length = min(8 * 1024**2, start + size - offset)
            for part, relative, n in segments(self.parts, offset, length):
                absolute = part['offset'] + relative
                key = hashlib.sha256(f'{absolute}:{n}'.encode()).hexdigest()
                path = self.out / 'ranges' / (key + '.raw')
                if key in self.pending:
                    self._finish(key)
                row = self.db.execute('select sha256,proof from ranges where id=?', (key,)).fetchone()
                if row:
                    if not path.exists() and getattr(self,'portable',None) is not None:
                        data=self.portable.read(key,absolute,n,row[0],json.loads(row[1]))
                    else:data = path.read_bytes()
                    if len(data) != n or hashlib.sha256(data).hexdigest() != row[0]:
                        raise ValueError('FREEZER_CACHE_CHANGED')
                    self.metrics['cache_reads'] += 1
                    ref = dict(offset=absolute, bytes=n, sha256=row[0], cloud=json.loads(row[1]))
                else:
                    # Capacity includes retained bytes whose Blob proof has not
                    # yet committed. Never fetch an unbounded ahead-of-proof tail.
                    while len(self.pending) >= self.max_pending_ranges:
                        self._finish(next(iter(self.pending)))
                    for pending in self.pending.values():
                        if pending['future'].done():
                            pending['future'].result()
                    began = time.monotonic()
                    with ArchiveGate(self.catalogue / 'source-gate', self.guard, wait=True):
                        entered=time.monotonic();self.metrics['gate_wait_seconds']+=entered-began
                        ids = []
                        def reserve():
                            self.guard(n)
                            i = uuid.uuid4().hex
                            ids.append(i)
                            with self.db:
                                self.db.execute('insert into attempts values(?,?,?)', (i, n, 'reserved'))
                            self.cloud.spend.reserve('read', 'freezer-' + i, bytes_=n)
                        reserve()
                        pinrow = self.db.execute('select body from sources where url=?', (part['url'],)).fetchone()
                        pin = json.loads(pinrow[0]) if pinrow else self.pins.get(part['url'])
                        requested=time.monotonic()
                        data, source = self.http.read(part, relative, n, pin, before_redirect=reserve, reuse_redirect=True)
                        self.metrics['http_seconds']+=time.monotonic()-requested
                    self.metrics['provider_seconds'] += time.monotonic() - began
                    self.metrics['requests'] += len(ids)
                    digest = hashlib.sha256(data).hexdigest()
                    working_identity=None
                    if getattr(self,'cloud_first',False):
                        path,working_identity=self.portable.stage(key,data,path)
                    else:
                        if path.exists() and path.read_bytes() != data:
                            raise ValueError('FREEZER_PARTIAL_CHANGED')
                        if not path.exists():
                            with path.open('xb') as f:
                                f.write(data);f.flush();os.fsync(f.fileno())
                    # Pin the validated provider identity before another source
                    # request; the range itself remains uncommitted until Blob.
                    with self.db:
                        self.db.execute('insert or ignore into sources values(?,?)', (part['url'], canonical(source)))
                    ref = dict(offset=absolute, bytes=n, sha256=digest)
                    future = self.uploads.submit(path)
                    self.pending[key] = dict(future=future, absolute=absolute, size=n,
                                            digest=digest, ids=ids, ref=ref,
                                            working_path=str(path) if working_identity is not None else None,
                                            working_identity=working_identity)
                    self.metrics['max_pending'] = max(self.metrics['max_pending'], len(self.pending))
                    self.calls += len(ids)
                    self.network_bytes += n
                self.last_refs.append(ref)
                output.append(data)
        return b''.join(output)

    def member(self, name, offset, size):
        if not self.read_ahead or not name.endswith('idx') or size <= 6:
            return super().member(name, offset, size)
        path = self.prefix + '/' + name
        if path not in self.files:
            raise ValueError('FREEZER_MEMBER_PENDING:' + name)
        entry = self.files[path]
        if entry['directory'] or offset < 0 or size < 0 or offset + size > entry['bytes']:
            raise ValueError('FREEZER_MEMBER_RANGE')
        unit = 1024**2
        pieces = []
        end = offset + size
        for begin in range(offset // unit * unit, end, unit):
            raw = self.read(entry['data_offset'] + begin, min(unit, entry['bytes'] - begin))
            pieces.append(raw[max(offset - begin, 0):min(end - begin, len(raw))])
        return b''.join(pieces)

    def close(self):
        try:
            self.drain()
        finally:
            self.uploads.close()
            self.metrics['upload_worker_seconds']=sum(sum(l.seconds) for l in self.uploads.lanes)
            self.metrics['uploads_completed']=sum(len(l.seconds) for l in self.uploads.lanes)
            self.metrics['source_inventory']=source_inventory_metrics()
            if getattr(self,'portable',None) is not None:self.metrics['portable_ranges']=self.portable.metrics
            atomic(self.out / f"pipeline-metrics-{self.metrics['started_at_ms']}.json", self.metrics)
            super().close()
