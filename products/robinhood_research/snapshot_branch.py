"""Follow a verified sparse header anchor using the original source allowance.

This branch does not advance the ordinary catalogue's cursor. It is only a
locator: native roots, independent source comparison and graph admission remain
mandatory before any chain record may be indexed.
"""
import argparse
import hashlib
import json
import sqlite3
import time
import fcntl
from pathlib import Path

from economic_machine.pump_research.research_cloud import Cloud
from economic_machine.pump_research.store import atomic, canonical
from economic_machine.arbitrum_research.snapshot_ranges import tar_header
from economic_machine.robinhood_research.snapshot_scout import Scout, DEADLINE, validate_anchor


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base',required=True); p.add_argument('--catalogue',required=True)
    p.add_argument('--out',required=True); p.add_argument('--anchor',required=True)
    p.add_argument('--anchor-sha',required=True); p.add_argument('--max-new-headers',type=int,default=4096)
    a = p.parse_args()
    if not 1<=a.max_new_headers<=4096: raise ValueError('BRANCH_BOUND')
    anchor_raw=Path(a.anchor).read_bytes()
    if len(anchor_raw)>4*1024**2 or hashlib.sha256(anchor_raw).hexdigest()!=a.anchor_sha:
        raise ValueError('BRANCH_ANCHOR_IDENTITY')
    anchor=json.loads(anchor_raw)
    headers=anchor['headers']
    if not headers or len(headers)>256:raise ValueError('BRANCH_ANCHOR_BOUND')
    cloud=Cloud(a.base);scout=Scout(a.catalogue,a.out,cloud)
    lock=(scout.out/'branch.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    db=sqlite3.connect(scout.out/'branch.sqlite3',timeout=30)
    db.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS identity(body TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS headers(offset INTEGER PRIMARY KEY,raw BLOB,body TEXT);
      CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY,next_offset INTEGER,finished INTEGER);
    ''')
    source_sha=hashlib.sha256(canonical(scout.identity).encode()).hexdigest()
    identity=canonical(dict(source_sha=source_sha,anchor_sha=a.anchor_sha,
        max_new_headers=a.max_new_headers,deadline_ms=DEADLINE,
        original_ledger=str(Path(a.catalogue)/'runtime.sqlite3')))
    existing=db.execute('SELECT body FROM identity').fetchall()
    if existing and existing!=[(identity,)]:raise ValueError('BRANCH_IDENTITY_CHANGED')
    previous=validate_anchor(anchor,scout.total,lambda digest: cloud.spend.db.execute(
        'SELECT bytes,verified FROM objects WHERE digest=?',(digest,)).fetchone())
    with db:
        db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(identity,))
        db.execute('INSERT OR IGNORE INTO cursor VALUES(1,?,0)',(previous,))
    state='LOCATING_TABLES';began=time.monotonic()
    saved=[json.loads(r[0]) for r in db.execute('SELECT body FROM headers')]
    names={r.get('name','').rsplit('/',1)[-1] for r in saved if r.get('history_candidate')}
    def publish():
        rows=[dict(entry=json.loads(body),raw_hex=bytes(raw).hex()) for raw,body in db.execute('SELECT raw,body FROM headers ORDER BY offset')]
        atomic(scout.out/'catalogue.json',dict(identity=json.loads(identity),anchor_sha=a.anchor_sha,source=scout.identity,headers=rows))
        proof=cloud.upload(scout.out/'catalogue.json')
        candidate=[r['entry'] for r in rows if r['entry'].get('history_candidate')]
        atomic(scout.out/'status.json',dict(state=state,at_ms=time.time_ns()//1_000_000,
            elapsed_seconds=time.monotonic()-began,new_headers=len(rows),
            next_offset=db.execute('SELECT next_offset FROM cursor').fetchone()[0],
            catalogue_blob=proof,history_candidates=candidate,chain_records_indexed=0,
            original_cursor_unchanged_by_branch=True,
            requests_total=scout.db.execute('SELECT count(*) FROM attempts').fetchone()[0],
            request_limit=20000,deadline_ms=DEADLINE))
    try:
        while db.execute('SELECT count(*) FROM headers').fetchone()[0]<a.max_new_headers:
            scout.guard()
            at,finished=db.execute('SELECT next_offset,finished FROM cursor').fetchone()
            if finished:state='BRANCH_END_MARKER';break
            raw=scout.fetch(at,512);entry=tar_header(raw,at,scout.total)
            if entry['end_marker'] and scout.fetch(at+512,512)!=bytes(512):
                raise ValueError('BRANCH_END_MARKER_MISMATCH')
            with db:
                db.execute('INSERT INTO headers VALUES(?,?,?)',(at,raw,canonical(entry)))
                db.execute('UPDATE cursor SET next_offset=?,finished=?',(entry['next_offset'],int(entry['end_marker'])))
            n=db.execute('SELECT count(*) FROM headers').fetchone()[0]
            if n%128==0 or entry.get('history_candidate'):
                publish();print(json.dumps(dict(headers=n,candidate=entry if entry.get('history_candidate') else None)),flush=True)
            # Stop once the four indexes are located. Data files must still be
            # catalogued and independently verified before extraction can start.
            if entry.get('history_candidate'):names.add(entry['name'].rsplit('/',1)[-1])
            if {'headers.cidx','bodies.cidx','receipts.cidx','hashes.ridx'}<=names:
                state='INDEX_LOCATIONS_FOUND_PENDING_EXTRACTION';break
        else:state='BRANCH_WINDOW_COMPLETE_TABLES_PENDING'
    except Exception as e:
        state='STOPPED_'+str(e)[:120]
        atomic(scout.out/('error-'+str(time.time_ns()//1_000_000)+'.json'),dict(type=type(e).__name__,error=str(e)[:200]))
        raise
    finally:
        try:publish()
        finally:db.close();lock.close();scout.close();cloud.close()


if __name__=='__main__':main()
