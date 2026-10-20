"""Replace only the unfinished, reproducible trade projection with v2.

Run with the core and map workers stopped. Compressed source ranges, completed
tables, grant, network ledger, and the old row-prefix digest are preserved.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
from economic_machine.pump_research.history_core import Scope,sha
from economic_machine.pump_research.store import atomic

def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);a=p.parse_args()
    base=Path(a.base);out=base/'evidence/core-trace-v1'
    for unit in ('economic-machine-pump-core.service','economic-machine-pump-map.service'):
        state=subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True).stdout.strip()
        if state not in ('inactive','failed'):raise ValueError('WORKER_STILL_ACTIVE')
    path=out/'pump_event_default.partial'
    if not path.exists() or (out/'pump_event_default.sqlite3').exists():raise ValueError('UNFINISHED_ONLY')
    with closing(sqlite3.connect(path)) as db:
        if db.execute("SELECT type FROM sqlite_master WHERE name='projection'").fetchone()!=('table',):raise ValueError('LEGACY_ONLY')
        checkpoint=db.execute('SELECT rows,hash FROM checkpoint WHERE id=1').fetchone()
        if db.execute('SELECT count(*) FROM projection').fetchone()[0]!=checkpoint[0]:raise ValueError('CHECKPOINT_MISMATCH')
    scope=Scope(base,out/'grant.json')
    try:
        raw=sum(p.stat().st_size for p in (out/'ranges').glob('*.range'))
        if raw<3217120145+2644975429:raise ValueError('RETAINED_SOURCE_REQUIRED')
        record=dict(action='rebuild_incomplete_derived_projection_only',old_bytes=path.stat().st_size,
            old_sha256=sha(path),old_committed_rows=checkpoint[0],old_copy_prefix_sha256=checkpoint[1],
            retained_compressed_bytes=raw,grant_sha256=scope.hash,network_reserved_bytes=scope.reserved(),
            replacement='dictionary-events-v2',original_source_retained=True,
            signatures_recoverable_from_original_copy_rows=True)
        report=out/'compact-rebuild-v2.json'
        if report.exists():raise ValueError('MIGRATION_RECORD_EXISTS')
        atomic(report,record)
        path.unlink()
        print(json.dumps(record))
    finally:scope.close()

if __name__=='__main__':main()
