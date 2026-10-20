"""Source-writer-only reuse of bounded inventory observations.

The writer and its two upload lanes keep independent Cloud/Spend instances.
Only the same existing <=2 second inventory observation is shared; every call
still executes the original guard and reads current free disk space.
"""
import os
from .freezer_archive_guard import InventoryCache
_CACHE=InventoryCache()
def install_source_guard(cloud,cache=None):
    enabled=os.environ.get("EM_FREEZER_SOURCE_SHARED_INVENTORY","0")
    if enabled not in ("0","1"):raise ValueError("SOURCE_INVENTORY_SETTING")
    if enabled=="0":return False
    cache=_CACHE if cache is None else cache
    original=cloud.guard_local
    if getattr(original,"_source_inventory_guard",False):raise ValueError("SOURCE_INVENTORY_ALREADY_INSTALLED")
    def guarded(extra):return cache.guard(cloud,original,extra)
    guarded._source_inventory_guard=True
    cloud.guard_local=guarded
    return True
def source_inventory_metrics():return _CACHE.snapshot()
