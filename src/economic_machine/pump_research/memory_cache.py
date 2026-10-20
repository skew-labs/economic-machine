"""Process-local, content-verified read cache. No prefetch, credentials or IO spend.

Only bytes requested by the index enter this cache. The host and cgroup limits
are sampled on access and every five seconds, including when queries go idle.
Eviction removes RAM copies only; source files and Blob objects are untouched.
"""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import threading
import time

GIB = 1024**3


def capacity(current, configured, available, total, group_current=0, group_max=None):
    reserve = max(8 * GIB, total // 8)
    spare = available - reserve
    target = current + (spare * 4 // 5 if spare >= 0 else spare)
    if group_max is not None:
        target = min(target, current + group_max - group_current - 256 * 1024**2)
    return max(0, min(configured, target))


def host_capacity(current, configured):
    mem = {a.rstrip(':'): int(b) * 1024 for a, b, *_ in
           (row.split() for row in Path('/proc/meminfo').read_text().splitlines())}
    group = None
    for row in Path('/proc/self/cgroup').read_text().splitlines():
        if row.startswith('0::'):
            group = Path('/sys/fs/cgroup') / row[3:].lstrip('/')
    used = 0; ceiling = None
    if group is not None:
        # Enforce the tightest ancestor as well as the leaf's own cap.
        limits = []
        for path in [group, *group.parents]:
            if path == Path('/sys'): break
            if (path / 'memory.max').exists():
                value = (path / 'memory.max').read_text().strip()
                if value != 'max':
                    limits.append(int(value) - int((path / 'memory.current').read_text()))
        if limits: ceiling = min(limits)
    return capacity(current, configured, mem['MemAvailable'], mem['MemTotal'], used, ceiling)


class VerifiedCache:
    def __init__(self, limit, sampler=host_capacity):
        self.limit = limit; self.sampler = sampler; self.rows = OrderedDict()
        self.lock = threading.RLock(); self.load_lock = threading.Lock()
        self.bytes = 0; self.hits = 0; self.misses = 0; self.loads = 0
        self.evictions = 0; self.avoided_verified_bytes = 0; self.effective_limit = 0
        self.next_sample = 0

    def trim(self, force=True):
        with self.lock:
            now=time.monotonic()
            if not force and now < self.next_sample: return
            try: self.effective_limit = self.sampler(self.bytes, self.limit)
            except (OSError, ValueError): self.effective_limit = 0
            self.next_sample=now+.25
            while self.rows and self.bytes > self.effective_limit:
                _, raw = self.rows.popitem(last=False)
                self.bytes -= len(raw); self.evictions += 1

    def _hit(self, key):
        raw = self.rows.get(key)
        if raw is not None:
            self.rows.move_to_end(key); self.hits += 1
            self.avoided_verified_bytes += len(raw)
        return raw

    def read(self, namespace, digest, size, loader):
        key = (namespace, digest, size)
        self.trim(force=False)
        with self.lock:
            raw = self._hit(key)
            if raw is not None: return raw
            self.misses += 1
        # One cold load per process prevents duplicate allocations/restores.
        with self.load_lock:
            with self.lock:
                raw = self._hit(key)
                if raw is not None: return raw
            raw = loader()
            if type(raw) is not bytes or len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('RESEARCH_RAM_CACHE_HASH')
            with self.lock: self.loads += 1
            self.trim()
            with self.lock:
                if size <= self.effective_limit:
                    while self.rows and self.bytes + size > self.effective_limit:
                        _, old = self.rows.popitem(last=False)
                        self.bytes -= len(old); self.evictions += 1
                    self.rows[key] = raw; self.bytes += size
            return raw

    def snapshot(self):
        with self.lock:
            return dict(pid=os.getpid(), at_ms=time.time_ns()//1_000_000,
                        configured_bytes=self.limit, effective_limit_bytes=self.effective_limit,
                        resident_verified_bytes=self.bytes, objects=len(self.rows),
                        hits=self.hits, misses=self.misses, verified_loads=self.loads,
                        evictions=self.evictions, avoided_verified_bytes=self.avoided_verified_bytes,
                        host_reserve_minimum_bytes=8*GIB, source_objects_deleted=False)


_cache = None
_init_lock = threading.Lock()


def shared_cache():
    global _cache
    with _init_lock:
        if _cache is None:
            limit = int(os.environ.get('EM_VERIFIED_RAM_CACHE_BYTES', '0'))
            if not 0 <= limit <= 32 * GIB: raise ValueError('RESEARCH_RAM_CACHE_BOUND')
            _cache = VerifiedCache(limit)
            if limit:
                def maintain():
                    while True:
                        _cache.trim()
                        destination = os.environ.get('EM_RAM_CACHE_STATUS')
                        if destination:
                            try:
                                from .store import atomic
                                atomic(Path(destination), _cache.snapshot())
                            except OSError: pass
                        time.sleep(5)
                threading.Thread(target=maintain, name='verified-cache-reclaim', daemon=True).start()
        return _cache
