"""Lossless dictionary encoding of the derived balance index, not raw deletion."""
from .store import canonical

def objects(s,maximum=10000):
    """Resume safely after any interruption; only remove verified duplicates."""
    from . import object_store as cas
    before=after=done=0
    rows=s.db.execute('SELECT hash,bytes FROM objects WHERE stored_bytes IS NULL ORDER BY rowid LIMIT ?', (maximum,)).fetchall()
    for offset in range(0,len(rows),256):
        with s.db:
            for row in rows[offset:offset+256]:
                digest=row['hash'];plain,packed=cas.paths(s.root,digest)
                raw=cas.read(s.root,digest)
                if len(raw)!=row['bytes']:raise ValueError('OBJECT_SIZE_MISMATCH')
                if packed.exists():size=packed.stat().st_size
                else:
                    data,codec=cas.encode(raw);size=len(data)
                    if codec=='deflate1':
                        s.guard(size)
                        cas.write(s.root,digest,data,codec)
                        if cas.read(s.root,digest)!=raw:raise ValueError('COMPACTION_VERIFY')
                # Remove only a byte-for-byte verified redundant representation.
                if packed.exists() and plain.exists():plain.unlink();cas.sync_dir(s.root)
                s.db.execute('UPDATE objects SET stored_bytes=? WHERE hash=?',(size,digest))
                s.set_state('object_bytes',s.state('object_bytes')-len(raw)+size)
                before+=len(raw);after+=size;done+=1
    return dict(objects=done,raw_bytes=before,stored_bytes=after,exact_hash_verified=done,raw_content_deleted=False)

def transfers(s):
    """Dictionary ids replace repeated text; compatibility view keeps queries."""
    if s.db.execute("SELECT type FROM sqlite_master WHERE name='transfers'").fetchone()[0]=='view':return dict(already_compacted=True)
    db=s.db;db.execute('BEGIN IMMEDIATE')
    try:
        before=db.execute('SELECT count(*) FROM transfers').fetchone()[0]
        db.execute('ALTER TABLE transfers RENAME TO transfers_legacy')
        db.execute('CREATE TABLE transfer_strings(id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE)')
        db.execute('CREATE TABLE transfer_observations(id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE REFERENCES observations(id))')
        for column in ('source','destination','mint','kind','authority'):
            db.execute(f'INSERT OR IGNORE INTO transfer_strings(value) SELECT DISTINCT {column} FROM transfers_legacy')
        db.execute('INSERT INTO transfer_observations(value) SELECT DISTINCT observation FROM transfers_legacy')
        db.execute('''CREATE TABLE transfer_cells(oid INTEGER REFERENCES transfer_observations(id),ix INTEGER,
            sid INTEGER REFERENCES transfer_strings(id),did INTEGER REFERENCES transfer_strings(id),
            mid INTEGER REFERENCES transfer_strings(id),amount TEXT,kid INTEGER REFERENCES transfer_strings(id),
            aid INTEGER REFERENCES transfer_strings(id),slot INTEGER,PRIMARY KEY(oid,ix)) WITHOUT ROWID''')
        db.execute('''INSERT INTO transfer_cells SELECT o.id,t.ix,s.id,d.id,m.id,t.amount,k.id,a.id,t.slot
            FROM transfers_legacy t JOIN transfer_observations o ON o.value=t.observation
            JOIN transfer_strings s ON s.value=t.source JOIN transfer_strings d ON d.value=t.destination
            JOIN transfer_strings m ON m.value=t.mint JOIN transfer_strings k ON k.value=t.kind
            JOIN transfer_strings a ON a.value=t.authority''')
        db.execute('CREATE INDEX transfer_inbound ON transfer_cells(did,slot DESC)')
        db.execute('CREATE INDEX transfer_outbound ON transfer_cells(sid,slot)')
        db.execute('CREATE INDEX transfer_slot ON transfer_cells(slot)')
        db.execute('''CREATE VIEW transfers AS SELECT o.value observation,t.ix,s.value source,d.value destination,
            m.value mint,t.amount,k.value kind,a.value authority,t.slot FROM transfer_cells t
            JOIN transfer_observations o ON o.id=t.oid JOIN transfer_strings s ON s.id=t.sid
            JOIN transfer_strings d ON d.id=t.did JOIN transfer_strings m ON m.id=t.mid
            JOIN transfer_strings k ON k.id=t.kid JOIN transfer_strings a ON a.id=t.aid''')
        after=db.execute('SELECT count(*) FROM transfer_cells').fetchone()[0]
        matched=db.execute('''SELECT count(*) FROM transfers_legacy t JOIN transfers n ON n.observation=t.observation AND n.ix=t.ix
            WHERE n.source=t.source AND n.destination=t.destination AND n.mint=t.mint AND n.amount=t.amount
            AND n.kind=t.kind AND n.authority=t.authority AND n.slot=t.slot''').fetchone()[0]
        if before!=after or before!=matched:raise ValueError('TRANSFER_COMPACTION_MISMATCH')
        db.execute('''CREATE TRIGGER transfer_insert INSTEAD OF INSERT ON transfers BEGIN
            INSERT OR IGNORE INTO transfer_observations(value) VALUES(NEW.observation);
            INSERT OR IGNORE INTO transfer_strings(value) VALUES(NEW.source),(NEW.destination),(NEW.mint),(NEW.kind),(NEW.authority);
            INSERT OR IGNORE INTO transfer_cells SELECT o.id,NEW.ix,s.id,d.id,m.id,NEW.amount,k.id,a.id,NEW.slot
            FROM transfer_observations o,transfer_strings s,transfer_strings d,transfer_strings m,transfer_strings k,transfer_strings a
            WHERE o.value=NEW.observation AND s.value=NEW.source AND d.value=NEW.destination AND m.value=NEW.mint AND k.value=NEW.kind AND a.value=NEW.authority;
        END''')
        db.execute('DROP TABLE transfers_legacy')
        result=dict(rows_before=before,rows_after=after,exact_rows_matched=matched,history_deleted=False,format='dictionary_ids_v1')
        s.set_state('transfer_compaction',result);db.commit();return result
    except BaseException:db.rollback();raise

def balances(s):
    if s.db.execute("SELECT type FROM sqlite_master WHERE name='balances'").fetchone()[0]=='view':
        return dict(already_compacted=True)
    db=s.db
    db.execute('BEGIN IMMEDIATE')
    try:
        before=db.execute('SELECT count(*) FROM balances').fetchone()[0]
        db.execute('ALTER TABLE balances RENAME TO balances_legacy')
        db.execute('CREATE TABLE balance_wallets(id INTEGER PRIMARY KEY,wallet TEXT NOT NULL UNIQUE)')
        db.execute('CREATE TABLE balance_observations(id INTEGER PRIMARY KEY,observation TEXT NOT NULL UNIQUE REFERENCES observations(id))')
        db.execute('CREATE TABLE balance_cells(oid INTEGER REFERENCES balance_observations(id),wid INTEGER REFERENCES balance_wallets(id),lamports TEXT NOT NULL,slot INTEGER NOT NULL,PRIMARY KEY(oid,wid)) WITHOUT ROWID')
        db.execute('INSERT INTO balance_wallets(wallet) SELECT DISTINCT wallet FROM balances_legacy')
        db.execute('INSERT INTO balance_observations(observation) SELECT DISTINCT observation FROM balances_legacy')
        db.execute('INSERT INTO balance_cells SELECT o.id,w.id,b.lamports,b.slot FROM balances_legacy b JOIN balance_wallets w ON w.wallet=b.wallet JOIN balance_observations o ON o.observation=b.observation')
        db.execute('CREATE INDEX balance_cells_wallet_slot ON balance_cells(wid,slot DESC)')
        db.execute('CREATE VIEW balances AS SELECT o.observation,w.wallet,b.lamports,b.slot FROM balance_cells b JOIN balance_observations o ON o.id=b.oid JOIN balance_wallets w ON w.id=b.wid')
        after=db.execute('SELECT count(*) FROM balance_cells').fetchone()[0]
        # Unique keys + exact matched cardinality prove row-for-row equality.
        matched=db.execute('''SELECT count(*) FROM balances_legacy old JOIN balance_observations o ON o.observation=old.observation
            JOIN balance_wallets w ON w.wallet=old.wallet JOIN balance_cells b ON b.oid=o.id AND b.wid=w.id
            WHERE b.lamports=old.lamports AND b.slot=old.slot''').fetchone()[0]
        if before!=after or before!=matched:raise ValueError('BALANCE_COMPACTION_MISMATCH')
        db.execute('''CREATE TRIGGER balance_insert INSTEAD OF INSERT ON balances BEGIN
            INSERT OR IGNORE INTO balance_wallets(wallet) VALUES(NEW.wallet);
            INSERT OR IGNORE INTO balance_observations(observation) VALUES(NEW.observation);
            INSERT OR IGNORE INTO balance_cells(oid,wid,lamports,slot)
            SELECT o.id,w.id,NEW.lamports,NEW.slot FROM balance_observations o,balance_wallets w
            WHERE o.observation=NEW.observation AND w.wallet=NEW.wallet;
        END''')
        db.execute('DROP TABLE balances_legacy')
        result=dict(rows_before=before,rows_after=after,exact_rows_matched=matched,
                    raw_evidence_deleted=False,history_deleted=False,format='dictionary_ids_v1')
        s.set_state('balance_compaction',result);db.commit();return result
    except BaseException:
        db.rollback();raise

def main():
    import argparse
    import fcntl
    import hashlib
    import time
    from .store import Store,atomic
    p=argparse.ArgumentParser(description='Offline lossless research storage maintenance')
    p.add_argument('--root',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    s=Store(a.root);locks=[]
    for name in ('tail','archive','shadow'):
        f=(s.root/(name+'.lock')).open('a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
    def size():
        return dict(objects=sum(x.stat().st_size for x in (s.root/'objects').iterdir() if x.is_file()),
                    database=sum(x.stat().st_size for x in s.root.glob('search.sqlite3*')))
    def identity():
        h=hashlib.sha256()
        for row in s.db.execute('SELECT hash,bytes FROM objects ORDER BY hash'):h.update(canonical(list(row)).encode()+b'\n')
        return h.hexdigest()
    start=time.monotonic();result=dict(before=size(),raw_identity_before=identity())
    totals=dict(objects=0,raw_bytes=0,stored_bytes=0,exact_hash_verified=0)
    while True:
        batch=objects(s,10000)
        for key in totals:totals[key]+=batch[key]
        print(canonical(dict(stage='objects',**totals)),flush=True)
        if batch['objects']<10000:break
    result['compression']=totals;result['transfers']=transfers(s)
    s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');s.db.execute('VACUUM');s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    result.update(after=size(),raw_identity_after=identity(),quick_check=s.db.execute('PRAGMA quick_check').fetchone()[0],
                  elapsed_seconds=time.monotonic()-start,raw_content_deleted=False,history_deleted=False)
    if result['raw_identity_before']!=result['raw_identity_after']:raise ValueError('OBJECT_MANIFEST_CHANGED')
    if result['quick_check']!='ok':raise ValueError('SQLITE_CHECK')
    s.guard();atomic(a.report,result);print(canonical(result),flush=True);s.db.close()

if __name__=='__main__':main()
