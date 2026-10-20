"""Bounded public-data projection. Run only on the Amsterdam research host."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from economic_machine.pump_research.history_dump import Ranges, copy_rows

SELECT = {
    'tokens': ['mint', 'creator_address', 'discovered_at', 'discovered_price',
               'last_trade_at', 'graduated_at', 'snapshots_count'],
    'rotations': ['id', 'mint', 'discovered_at', 'graduated_at', 'last_trade_at',
                  'rotated_at', 'unique_buyers', 'total_volume_sol'],
    'fingerprint_snapshots': ['id', 'mint', 'timestamp', 'phase', 'trigger_reason',
                              'total_volume_sol', 'total_trade_count',
                              'unique_wallets', 'is_deathbed', 'current_price'],
}
TEXT = {'mint', 'creator_address', 'phase', 'trigger_reason', 'is_deathbed'}
INT = {'id', 'discovered_at', 'last_trade_at', 'graduated_at', 'snapshots_count',
       'rotated_at', 'timestamp', 'total_trade_count', 'unique_wallets', 'unique_buyers'}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base', required=True)
    a = p.parse_args()
    base = Path(a.base)
    out = base/'evidence/history-alpha-v1'
    target = out/'fingerprint-projection.sqlite3'
    if target.exists():
        raise ValueError('PRESERVE_EXISTING_PROJECTION')
    toc = json.loads((out/'fingerprint-toc.json').read_text())
    files = json.loads((out/'fingerprint-files.json').read_text())
    parts = [dict(url=f['download_url'], size=f['size']) for f in files
             if f['name'].startswith('fingerprinter_db.dump')]
    source = Ranges(base, out/'ranges', parts)
    db = sqlite3.connect(str(target)+'.partial')
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute('PRAGMA synchronous=FULL')
    report = dict(started=time.time(), dataset_commit=json.loads((out/'fingerprint-commit.json').read_text())['sha'],
                  tables={}, sql_from_archive_executed=False, independently_chain_verified=False)
    for name, keys in SELECT.items():
        entry = next(x for x in toc['entries'] if x['tag']==name and x['description']=='TABLE DATA')
        end = min(x['offset'] for x in toc['entries'] if x['offset']>entry['offset'])
        raw = source.fetch(entry['offset'], end-entry['offset'])
        schema = ','.join('"'+k+'" '+('TEXT' if k in TEXT else 'INTEGER' if k in INT else 'REAL') for k in keys)
        db.execute('CREATE TABLE '+name+'('+schema+')')
        n, digest = 0, hashlib.sha256()
        batch = []
        for row in copy_rows(raw, entry):
            batch.append(tuple(row[k] for k in keys))
            digest.update(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()+b'\n')
            n += 1
            if n > 2000000:
                raise ValueError('DATASET_ROW_BOUND')
            if len(batch)==2000:
                db.executemany('INSERT INTO '+name+' VALUES('+','.join('?' for _ in keys)+')', batch)
                batch.clear()
        db.executemany('INSERT INTO '+name+' VALUES('+','.join('?' for _ in keys)+')', batch)
        db.execute('CREATE INDEX '+name+'_mint ON '+name+'(mint)')
        db.commit()
        report['tables'][name] = dict(rows=n, compressed_range_bytes=len(raw),
                                      compressed_sha256=hashlib.sha256(raw).hexdigest(),
                                      decoded_rows_sha256=digest.hexdigest())
        print(name, report['tables'][name], flush=True)
        del raw
    db.execute('CREATE INDEX snapshots_time ON fingerprint_snapshots(mint,timestamp,id)')
    db.commit()
    db.close()
    Path(str(target)+'.partial').rename(target)
    report.update(finished=time.time(), projection_bytes=target.stat().st_size)
    (out/'extraction.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    main()
