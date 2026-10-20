"""Bounded upload lanes; originals survive until every readback is verified."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import queue
import threading
import uuid


def upload_parts(base,path,size,block_size,workers,cloud_factory,progress=None,guard=None):
    if type(workers) is not int or not 1<=workers<=4:raise ValueError('ARCHIVE_WORKERS')
    if type(size) is not int or size<=0:raise ValueError('ARCHIVE_SIZE')
    path=Path(path);events=queue.Queue(maxsize=workers*2);stop=threading.Event();guard_lock=threading.Lock()
    def shared_guard(extra):
        # Inventory has the same two-second freshness as the serial path.
        # Avoid four simultaneous directory walks; spend connections stay private.
        with guard_lock:guard(extra)
    def lane(number):
        cloud=cloud_factory(base)
        if guard is not None:cloud.guard_local=shared_guard
        staging=cloud.out/'staging';staging.mkdir(exist_ok=True)
        try:
            with path.open('rb') as source:
                for offset in range(number*block_size,size,workers*block_size):
                    if stop.is_set():return
                    wanted=min(block_size,size-offset);cloud.guard_local(wanted+1024**2)
                    source.seek(offset);raw=source.read(wanted)
                    if len(raw)!=wanted:raise ValueError('ARCHIVE_SOURCE_SHORT_READ')
                    digest=hashlib.sha256(raw).hexdigest()
                    temporary=staging/('upload-'+uuid.uuid4().hex+'.part')
                    with temporary.open('xb') as target:
                        target.write(raw);target.flush();os.fsync(target.fileno())
                    row=cloud.upload(temporary,expected=digest)
                    if row!={'sha256':digest,'bytes':wanted}:raise ValueError('ARCHIVE_UPLOAD_IDENTITY')
                    temporary.unlink() # Only the verified working duplicate.
                    while not stop.is_set():
                        try:events.put(dict(offset=offset,**row),timeout=.1);break
                        except queue.Full:continue
        finally:cloud.close()
    rows={};completed=0
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='blob-readback') as pool:
        futures=[pool.submit(lane,n) for n in range(workers)]
        try:
            while not all(f.done() for f in futures) or not events.empty():
                for future in futures:
                    if future.done():future.result()
                try:row=events.get(timeout=.1)
                except queue.Empty:continue
                if row['offset'] in rows:raise ValueError('ARCHIVE_DUPLICATE_OFFSET')
                rows[row['offset']]=row;completed+=row['bytes']
                if progress:progress(completed,size)
            for future in futures:future.result()
        finally:stop.set()
    ordered=[rows[n] for n in sorted(rows)]
    offset=0
    for row in ordered:
        if row['offset']!=offset:raise ValueError('ARCHIVE_MISSING_BLOCK')
        offset+=row['bytes']
    if offset!=size:raise ValueError('ARCHIVE_INCOMPLETE')
    return ordered
