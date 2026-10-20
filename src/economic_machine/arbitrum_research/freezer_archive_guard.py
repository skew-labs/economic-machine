"""Coalesce archive-worker inventory scans within Cloud's existing two seconds.

Only byte-count observations are shared. Every caller keeps its own Cloud/Spend
and executes the original admission check, including a fresh host-free-space
lookup. Nothing is persisted, and no network operation runs under this lock.
"""
from collections import OrderedDict
import math
import os
from pathlib import Path
import re
import stat
import threading
import time


class InventoryCache:
    def __init__(self, maximum=8):
        if type(maximum) is not int or not 1<=maximum<=32:
            raise ValueError('ARCHIVE_INVENTORY_CACHE_BOUND')
        self.maximum=maximum
        self.lock=threading.Lock()
        self.entries=OrderedDict()
        self.metrics=dict(calls=0,cache_hits=0,refreshes=0,errors=0,
                          wait_seconds=0.0,original_guard_seconds=0.0)

    def key(self,cloud):
        root=Path(cloud.base).resolve(strict=True);identity=root.stat()
        grant_hash=cloud.spend.hash
        if not stat.S_ISDIR(identity.st_mode) or type(grant_hash) is not str or re.fullmatch('[0-9a-f]{64}',grant_hash) is None:
            raise ValueError('ARCHIVE_INVENTORY_IDENTITY')
        return os.getpid(),str(root),identity.st_dev,identity.st_ino,grant_hash

    def guard(self,cloud,original,extra):
        key=self.key(cloud)
        waiting=time.perf_counter()
        with self.lock:
            self.metrics['wait_seconds']+=time.perf_counter()-waiting
            self.metrics['calls']+=1
            now=time.monotonic();entry=self.entries.get(key)
            hit=entry is not None and 0<=now-entry[1]<=2
            if hit:
                cloud.local_bytes,cloud.last_storage_check=entry
                self.entries.move_to_end(key)
                self.metrics['cache_hits']+=1
            else:
                # Force the original guard to perform its own full inventory.
                # This also prevents a stale per-instance observation extending
                # the process-wide freshness window after an entry expires.
                cloud.last_storage_check=-math.inf
            before=cloud.last_storage_check;began=time.perf_counter()
            try:
                original(extra)
                completed=cloud.last_storage_check;size=cloud.local_bytes
                if (type(size) is not int or size<0 or not isinstance(completed,(int,float))
                        or not math.isfinite(completed) or not 0<=time.monotonic()-completed<=2):
                    raise ValueError('ARCHIVE_INVENTORY_OBSERVATION')
                # Preserve the actual original scan completion timestamp. A hit
                # never renews it. A slow original guard may have refreshed it.
                self.entries[key]=(size,completed)
                self.entries.move_to_end(key)
                while len(self.entries)>self.maximum:self.entries.popitem(last=False)
            except BaseException:
                # A failed scan/admission cannot publish a new shared observation.
                # Eviction also prevents reuse after a failed fresh floor check.
                self.entries.pop(key,None)
                self.metrics['errors']+=1
                raise
            finally:
                if cloud.last_storage_check!=before and math.isfinite(cloud.last_storage_check):
                    self.metrics['refreshes']+=1
                self.metrics['original_guard_seconds']+=time.perf_counter()-began

    def snapshot(self):
        with self.lock:return dict(self.metrics,entries=len(self.entries),maximum=self.maximum)


_CACHE=InventoryCache()


def install_archive_guard(cloud,cache=None):
    enabled=os.environ.get('EM_FREEZER_ARCHIVE_SHARED_INVENTORY','0')
    if enabled not in ('0','1'):raise ValueError('ARCHIVE_INVENTORY_SETTING')
    if enabled=='0':return False
    cache=_CACHE if cache is None else cache
    original=cloud.guard_local
    if getattr(original,'_archive_inventory_guard',False):
        raise ValueError('ARCHIVE_INVENTORY_ALREADY_INSTALLED')
    def guarded(extra):return cache.guard(cloud,original,extra)
    guarded._archive_inventory_guard=True
    cloud.guard_local=guarded
    return True


def inventory_metrics():
    return _CACHE.snapshot()
