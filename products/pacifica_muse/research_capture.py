#!/usr/bin/env python3
"""Read existing public snapshots/receipt journal into an isolated research DB.

No venue client, signer, socket consumer or live policy writer is present here.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))
from economic_machine.muse_learning import Store
from economic_machine.muse_maker import unique
from economic_machine.muse_policy_worker import atomic, now_ms, observer, read_json


def separate(root, *sources):
    root=Path(root).resolve()
    for source in sources:
        source=Path(source).resolve()
        if root==source or root in source.parents or source in root.parents:
            raise ValueError('research output must be separate from live input')
    return root


def journal(store, path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno());inode=f'{info.st_dev}:{info.st_ino}'
        if info.st_size>256*1024*1024:raise ValueError('research journal byte limit')
        row=store.db.execute('SELECT inode,offset,version FROM cursor WHERE id=1').fetchone()
        offset,version=(row[1],row[2]) if row else (0,0)
        if row and (row[0]!=inode or info.st_size<offset):raise ValueError('source journal replaced/truncated')
        stream.seek(offset)
        for _ in range(4096):
            line=stream.readline(131073)
            if len(line)>131072:raise ValueError('journal record byte limit')
            if not line or not line.endswith(b'\n'):break
            event=json.loads(line,object_pairs_hook=unique)
            stamp=event.get('at_ms')
            if type(stamp) is not int or stamp<store.lease['start_ms']:raise ValueError('source event outside session')
            if stamp<=store.lease['deadline_ms']:version=store.ingest(event,version)
            offset=stream.tell()
            with store.db:
                store.db.execute('INSERT OR REPLACE INTO cursor VALUES(1,?,?,?)',(inode,offset,version))
        return offset


def snapshot(store, report):
    store.db.execute('CREATE TABLE IF NOT EXISTS snapshot_report(id INTEGER PRIMARY KEY,body TEXT)')
    with store.db:store.db.execute('INSERT OR REPLACE INTO snapshot_report VALUES(1,?)',(json.dumps(report,sort_keys=True),))
    temp=store.root/'snapshot.tmp.sqlite';temp.unlink(missing_ok=True)
    target=sqlite3.connect(temp)
    try:store.db.backup(target)
    finally:target.close()
    os.replace(temp,store.root/'snapshot.sqlite')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live-root',type=Path,required=True)
    p.add_argument('--observer',type=Path,required=True)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=300)
    args=p.parse_args()
    if not 1<=args.seconds<=3600:p.error('bounded capture must be 1..3600 seconds')
    os.umask(0o077);root=separate(args.root,args.live_root,args.observer);root.mkdir(parents=True,exist_ok=True)
    lock=os.open(root/'capture.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    lease=read_json(args.live_root/'session-start.json');store=Store(root,lease)
    end=min(now_ms()+args.seconds*1000,lease['deadline_ms']);last_snapshot=0;error=None
    try:
        while now_ms()<end:
            journal(store,args.live_root/'events.jsonl')
            state=read_json(args.live_root/'status.json')
            try:obs=observer(args.observer)
            except (OSError,ValueError):obs={} # Optional analysis is unavailable.
            store.frame(state,obs,now_ms())
            if now_ms()-last_snapshot>=2000:
                report=dict(store.report(),state='CAPTURING',updated_ms=now_ms(),capture_end_ms=end)
                snapshot(store,report);atomic(root/'status.json',report);last_snapshot=now_ms()
            time.sleep(.2)
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)[:160]
    finally:
        report=dict(store.report(),state='FAILED' if error else 'COMPLETE',error=error,updated_ms=now_ms(),capture_end_ms=end)
        snapshot(store,report);atomic(root/'status.json',report);store.close();os.close(lock)
    print(json.dumps(report,sort_keys=True))
    return 1 if error else 0


if __name__=='__main__':raise SystemExit(main())
