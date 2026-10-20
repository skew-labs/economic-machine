"""Locate candidate tar members without claiming a complete archive catalogue.

Sparse headers can be coincidental bytes in a file body. They are leads until
their linked successor headers and the chain's native data roots are checked.
The ordinary catalogue cursor is never advanced by this reader.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from economic_machine.arbitrum_research.freezer_source import segments
from economic_machine.arbitrum_research.snapshot_ranges import HTTP, tar_header
from economic_machine.pump_research.archive_gate import ArchiveGate
from economic_machine.pump_research.store import atomic, canonical

DEADLINE = 1792351694298
MAX_READ = 8 * 1024**2


def validate_anchor(anchor, total, verified):
    headers=anchor.get('headers')
    if not isinstance(headers,list) or not 1<=len(headers)<=256:
        raise ValueError('BRANCH_ANCHOR_BOUND')
    previous=anchor.get('start')
    for row in headers:
        raw=bytes.fromhex(row['raw_hex']);entry=row['entry']
        if tar_header(raw,entry['header_offset'],total)!=entry:
            raise ValueError('BRANCH_ANCHOR_HEADER')
        if previous!=entry['header_offset']:raise ValueError('BRANCH_ANCHOR_GAP')
        proof=row['proof']
        if hashlib.sha256(raw).hexdigest()!=proof['sha256'] or proof['bytes']!=512 or verified(proof['sha256'])!=(512,1):
            raise ValueError('BRANCH_ANCHOR_BLOB')
        previous=entry['next_offset']
    if previous!=anchor.get('next_offset'):raise ValueError('BRANCH_ANCHOR_CURSOR')
    return previous


def candidates(raw, start, total):
    if start < 0 or start % 512 or len(raw) > MAX_READ or start + len(raw) > total:
        raise ValueError('SCOUT_RANGE')
    found = []
    for at in range(0, len(raw)-511, 512):
        header = raw[at:at+512]
        if header[257:262] != b'ustar':
            continue
        try:
            entry = tar_header(header, start+at, total)
        except ValueError:
            continue
        if not entry['end_marker']:
            found.append(entry)
    return found


class Scout:
    def __init__(self, catalogue, out, cloud):
        self.catalogue = Path(catalogue)
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.cloud = cloud
        self.http = HTTP()
        self.db = sqlite3.connect(self.catalogue/'runtime.sqlite3', timeout=30)
        self.db.execute('PRAGMA busy_timeout=30000')
        self.identity = json.loads(self.db.execute('SELECT body FROM identity').fetchone()[0])
        if self.identity['deadline_ms'] != DEADLINE:
            raise ValueError('SCOUT_AUTHORITY')
        self.parts = self.identity['parts']
        self.total = sum(p['bytes'] for p in self.parts)
        self.before_cursor = self.db.execute('SELECT offset,finished FROM cursor').fetchone()
        self.ranges = []

    def guard(self):
        if time.time_ns()//1_000_000 >= DEADLINE:
            raise ValueError('SCOUT_DEADLINE')
        self.cloud.guard_local(MAX_READ*2)

    def reserve(self, start, size):
        self.guard()
        attempt = 'scout-'+uuid.uuid4().hex
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT count(*) FROM attempts').fetchone()[0] >= 20000:
                raise ValueError('CATALOGUE_REQUEST_BOUND')
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?)',
                            (attempt, start, size, 'reserved'))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.cloud.spend.reserve('read', attempt, bytes_=size)
        return attempt

    def fetch(self, start, size):
        self.guard()
        if start % 512 or not 0 < size <= MAX_READ or start+size > self.total:
            raise ValueError('SCOUT_RANGE')
        data = []
        for part, relative, count in segments(self.parts, start, size):
            with ArchiveGate(self.catalogue/'source-gate', self.guard, wait=True):
                ids = []
                def reserve():
                    ids.append(self.reserve(part['offset']+relative, count))
                reserve()
                row = self.db.execute('SELECT body FROM sources WHERE url=?', (part['url'],)).fetchone()
                pin = json.loads(row[0]) if row else None
                raw, source = self.http.read(part, relative, count, pin,
                    before_redirect=reserve, reuse_redirect=True)
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO sources VALUES(?,?)', (part['url'], canonical(source)))
                    self.db.executemany("UPDATE attempts SET state='received' WHERE id=?", [(i,) for i in ids])
            data.append(raw)
        return b''.join(data)

    def read(self, start, size):
        raw = self.fetch(start, size)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.out/f'{start}-{size}-{digest}.raw'
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError('SCOUT_SOURCE_CHANGED')
        else:
            with path.open('xb') as f:
                import os
                f.write(raw); f.flush(); os.fsync(f.fileno())
        proof = self.cloud.upload(path)
        entry = dict(start=start, bytes=size, sha256=digest, blob=proof,
                     candidates=candidates(raw, start, self.total))
        self.ranges.append(entry)
        atomic(self.out/'status.json', dict(state='SCOUTING', at_ms=time.time_ns()//1_000_000,
            ranges=self.ranges, source=self.identity,
            source_attempts=self.db.execute('SELECT count(*) FROM attempts').fetchone()[0],
            catalogue_cursor_not_modified=True, chain_records_indexed=0,
            sparse_header_is_only_a_lead=True))
        return raw, entry

    def close(self):
        self.http.close()
        self.db.close()
