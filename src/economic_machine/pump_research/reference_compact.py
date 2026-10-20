"""Verify duplicate research bytes against retained originals before deduplication.

Never unlinks Ticking originals, capsules, manifests, or Blob objects. Committing
the reference precedes unlinking a redundant CAS copy; interruption is resumable.
"""
import argparse
import fcntl
import hashlib
from pathlib import Path
import time
from . import object_store as cas
from .store import Store,atomic,canonical

def identity(s):
    h=hashlib.sha256()
    for row in s.db.execute('SELECT hash,bytes FROM objects ORDER BY hash'):
        h.update(canonical(list(row)).encode()+b'\n')
    return h.hexdigest()

def sizes(s):
    files=list((s.root/'objects').iterdir())+list(s.root.glob('search.sqlite3*'))
    return dict(logical=sum(p.stat().st_size for p in files if p.is_file()),
                allocated=sum(p.stat().st_blocks*512 for p in files if p.is_file()))

def compact(s,ticking_root):
    reader=s.originals(ticking_root);root=str(reader.root)
    # Persist the schedule so the input query cannot change under its own writes.
    s.db.execute('CREATE TEMP TABLE ref_work AS SELECT o.hash,o.bytes,e.locator FROM objects o JOIN evidence e ON e.hash=o.hash LEFT JOIN archive_objects a ON a.hash=o.hash WHERE a.hash IS NULL AND e.source=\'ticking\' GROUP BY o.hash ORDER BY e.locator')
    before=sizes(s);digest=identity(s);done=freed=0;start=time.monotonic()
    last=0
    while rows:=s.db.execute('SELECT rowid,* FROM ref_work WHERE rowid>? ORDER BY rowid LIMIT 256',(last,)).fetchall():
        verified=[]
        for rowid,h,length,locator in rows:
            segment,offset,sequence=locator.split(':');offset=int(offset);sequence=int(sequence)
            raw,seq,_,end=reader.frame(segment,offset,reader.size(segment))
            if seq!=sequence or len(raw)!=length or hashlib.sha256(raw).hexdigest()!=h or cas.read(s.root,h)!=raw:
                raise ValueError('REFERENCE_COMPACTION_EXACT_MISMATCH')
            verified.append((h,root,segment,offset,end,seq))
        s.db.execute('BEGIN IMMEDIATE')
        with s.db:
            for ref in verified:
                h=ref[0];old=s.db.execute('SELECT stored_bytes,bytes FROM objects WHERE hash=?',(h,)).fetchone()
                s.db.execute('INSERT INTO archive_objects VALUES(?,?,?,?,?,?)',ref)
                s.db.execute('UPDATE objects SET stored_bytes=0 WHERE hash=?',(h,))
                s.set_state('object_bytes',s.state('object_bytes')-(old[0] if old[0] is not None else old[1]))
        for ref in verified:
            for p in cas.paths(s.root,ref[0]):
                if p.exists():freed+=p.stat().st_size;p.unlink()
        cas.sync_dir(s.root);done+=len(verified)
        last=rows[-1][0]
        if done%16384==0:
            s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            print(canonical(dict(verified=done,duplicate_bytes_removed=freed)),flush=True)
    s.db.execute('DROP TABLE ref_work')
    # Recover a crash after reference commit but before duplicate unlink.
    recovered=0
    for r in s.db.execute('SELECT * FROM archive_objects'):
        paths=cas.paths(s.root,r['hash'])
        if any(p.exists() for p in paths):
            if cas.read(s.root,r['hash'])!=s.read_object(r['hash']):raise ValueError('REFERENCE_DUPLICATE_MISMATCH')
            for p in paths:
                if p.exists():freed+=p.stat().st_size;p.unlink()
            recovered+=1
    cas.sync_dir(s.root)
    physical=sum(p.stat().st_size for p in (s.root/'objects').iterdir() if p.is_file())
    with s.db:
        accounted=s.db.execute('SELECT coalesce(sum(coalesce(stored_bytes,bytes)),0) FROM objects').fetchone()[0]
        s.set_state('object_bytes',accounted)
    s.orphan_bytes=max(0,physical-accounted)
    after_digest=identity(s)
    if digest!=after_digest:raise ValueError('REFERENCE_OBJECT_IDENTITY_CHANGED')
    result=dict(before=before,after=sizes(s),exact_objects_verified=done,crash_duplicates_recovered=recovered,
                duplicate_bytes_removed=freed,raw_identity_before=digest,raw_identity_after=after_digest,
                elapsed_seconds=time.monotonic()-start,cold_capsules_loaded=reader.cold_loads,
                blob_requests_reserved=reader.remote_requests,ticking_originals_deleted=0,blob_objects_deleted=0,
                logical_records_deleted=0,research_read_dependency='Ticking original retention and authenticated Blob readback')
    reader.close();s.db.commit();return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--ticking-root',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    s=Store(a.root);s.db.execute('PRAGMA busy_timeout=30000');locks=[]
    for name in ('tail','archive','shadow'):
        f=(s.root/(name+'.lock')).open('a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
    before_path=Path(a.report).with_suffix('.before.json')
    if not before_path.exists():
        atomic(before_path,dict(at=time.time(),size=sizes(s),raw_identity=identity(s),
                               objects=s.db.execute('SELECT count(*) FROM objects').fetchone()[0],
                               references=s.db.execute('SELECT count(*) FROM archive_objects').fetchone()[0]))
    result=compact(s,a.ticking_root)
    s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    result['after_checkpoint']=sizes(s);result['quick_check']=s.db.execute('PRAGMA quick_check').fetchone()[0]
    if result['quick_check']!='ok':raise ValueError('REFERENCE_DATABASE_INTEGRITY')
    s.guard();atomic(a.report,result);print(canonical(result),flush=True)

if __name__=='__main__':main()
