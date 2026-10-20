"""Our address -> signed transaction index over reconstructed public blocks.

All account keys, including loaded ALT accounts and failed transactions, are
indexed. Content verification is not consensus verification. Coverage is per
decoded block; absence in this partial index never proves no transaction.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import time
from .archive import nodes,link,dataframe,decompress,BASE
from .wire import raw_transaction,extract
from .store import address,decode,encode,canonical
from .precision import enrich_transaction,owner_evidence

RAW_COMMIT_BLOCK_SQL="coalesce(json_extract(receipt,'$.source'),'')!='ticking_retained'"


def install_commit_revision_tracking(db):
    """Constant-time invalidation; never count the live historical index per UI read."""
    db.execute('''CREATE TABLE IF NOT EXISTS commit_source_revision(
        id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,identity TEXT NOT NULL)''')
    db.execute('INSERT OR IGNORE INTO commit_source_revision VALUES(1,0,lower(hex(randomblob(16))))')
    for table in ('blocks','transactions','tx_commit_evidence','commit_cache_audits'):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
        for operation in ('INSERT','UPDATE','DELETE'):
            name='commit_revision_'+table+'_'+operation.lower()
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation} ON {table}
                BEGIN UPDATE commit_source_revision SET version=version+1 WHERE id=1; END''')


def transfer_commit_status(ix,evidence):
    """Instruction status after the caller establishes transaction success.

    Missing/malformed historical policy evidence cannot validate CPI effects.
    A rejection here means the attribution is inadmissible, not necessarily that
    the instruction is proven reverted (logs may simply be unavailable).
    """
    if type(ix) is not int or ix<0:return 'rejected'
    if ix%65536==0:return 'committed'
    if not isinstance(evidence,dict) or type(evidence.get('version')) is not int or evidence['version']!=1:return 'legacy_unchecked'
    orders=evidence.get('committed_inner_orders')
    if (not isinstance(orders,list) or len(orders)>=65536 or
        any(type(n) is not int or n<0 or n%65536==0 for n in orders) or
        len(set(orders))!=len(orders)):
        return 'legacy_unchecked'
    return 'committed' if ix in orders else 'rejected'


def parse_index_block(raw,slot,meta_type,*,metadata_decompress=decompress):
    ns=nodes(raw);blocks=[v for v in ns.values() if v[0]==2]
    if len(blocks)!=1 or blocks[0][1]!=slot:raise ValueError('INDEX_BLOCK_SLOT')
    block=blocks[0];seen=set();txs=[];errors=[]
    for el in block[3]:
        entry=ns[link(el)]
        if entry[0]!=1:raise ValueError('INDEX_ENTRY_TYPE')
        for tl in entry[3]:
            cid=link(tl);n=ns[cid]
            if cid in seen or n[0]!=0 or n[3]!=slot:raise ValueError('INDEX_TX_MEMBERSHIP')
            seen.add(cid)
            try:
                sig,keys,ix=raw_transaction(dataframe(n[1],ns))
                meta_raw=dataframe(n[2],ns)
                if not meta_raw:raise ValueError('INDEX_MISSING_METADATA')
                meta=meta_type.FromString(metadata_decompress(meta_raw))
                tx=extract(sig,keys,ix,meta,slot,n[4] if len(n)>4 and n[4] is not None else len(seen)-1)
                tx['account_keys']=[encode(k) for k in keys]+[encode(bytes(k)) for k in [*meta.loaded_writable_addresses,*meta.loaded_readonly_addresses]]
                enrich_transaction(tx,meta)
                txs.append(tx)
            except (ValueError,IndexError,KeyError) as e:errors.append(str(e)[:100])
    return dict(slot=slot,time=block[4][1],transactions=txs,decode_errors=errors,node_count=len(ns),transaction_nodes=len(seen))


class AddressIndex:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=15);self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA cache_size=-16384;
          CREATE TABLE IF NOT EXISTS blocks(id INTEGER PRIMARY KEY,slot INTEGER,stamp INTEGER,hash TEXT,
            state TEXT,tx_nodes INTEGER,decoded INTEGER,errors TEXT,receipt TEXT,UNIQUE(slot,hash));
          CREATE TABLE IF NOT EXISTS keys(id INTEGER PRIMARY KEY,key BLOB UNIQUE);
          CREATE INDEX IF NOT EXISTS block_slot ON blocks(slot,stamp);
          CREATE INDEX IF NOT EXISTS block_coverage ON blocks(state,slot);
          CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY,block INTEGER,signature BLOB,
            slot INTEGER,tx_order INTEGER,stamp INTEGER,successful INTEGER,UNIQUE(block,signature));
          CREATE INDEX IF NOT EXISTS transaction_signature_slot ON transactions(signature,slot);
          CREATE TABLE IF NOT EXISTS appearances(account INTEGER,slot INTEGER,tx INTEGER,
            PRIMARY KEY(account,slot,tx)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY,tx INTEGER,ix INTEGER,source INTEGER,
            destination INTEGER,mint TEXT,amount TEXT,kind TEXT,authority TEXT,UNIQUE(tx,ix));
          CREATE INDEX IF NOT EXISTS outgoing ON transfers(source,id);
          CREATE INDEX IF NOT EXISTS incoming ON transfers(destination,id);
          CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY,tx INTEGER,ix INTEGER,wallet INTEGER,
            mint TEXT,action TEXT,UNIQUE(tx,ix,wallet,action));
          CREATE INDEX IF NOT EXISTS actor ON activity(wallet,id);
          CREATE TABLE IF NOT EXISTS stream_dead_slots(slot INTEGER PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS token_owners(tx INTEGER,account TEXT,phase TEXT,owner TEXT,
            mint TEXT,amount TEXT,decimals INTEGER,PRIMARY KEY(tx,account,phase)) WITHOUT ROWID;
          CREATE INDEX IF NOT EXISTS owner_lookup ON token_owners(owner,tx);
          CREATE TABLE IF NOT EXISTS tx_precision(tx INTEGER PRIMARY KEY,version INTEGER,errors TEXT);
          CREATE TABLE IF NOT EXISTS tx_commit_evidence(tx INTEGER,version INTEGER,body TEXT,
            PRIMARY KEY(tx,version)) WITHOUT ROWID;
        ''')
        with self.db:
            self.db.execute('CREATE INDEX IF NOT EXISTS raw_commit_blocks ON blocks(id) WHERE '+RAW_COMMIT_BLOCK_SQL)
            install_commit_revision_tracking(self.db)
    def enrich(self,tid,tx):
        evidence=tx.get('instruction_commit_evidence')
        if evidence:
            body=canonical(evidence)
            prior=self.db.execute('SELECT body FROM tx_commit_evidence WHERE tx=? AND version=?',(tid,evidence['version'])).fetchone()
            if prior and prior[0]!=body:raise ValueError('INDEX_COMMIT_EVIDENCE_CONFLICT')
            self.db.execute('INSERT OR IGNORE INTO tx_commit_evidence VALUES(?,?,?)',(tid,evidence['version'],body))
        if not tx.get('precision_version'):return
        if self.db.execute('SELECT 1 FROM tx_precision WHERE tx=? AND version>=?',(tid,tx['precision_version'])).fetchone():return
        for s in tx.get('token_owner_snapshots',[]):
            self.db.execute('INSERT OR REPLACE INTO token_owners VALUES(?,?,?,?,?,?,?)',
                (tid,s['account'],s['phase'],s['owner'],s['mint'],s['amount'],s['decimals']))
        for e in tx.get('launches',[]):
            if e['payer']!=e['creator']:
                self.db.execute("UPDATE activity SET action='launch_payer' WHERE tx=? AND ix=? AND mint=? AND wallet=(SELECT id FROM keys WHERE key=?) AND action='create'",
                    (tid,e['ix'],e['mint'],decode(e['payer'])))
        self.db.execute('INSERT OR REPLACE INTO tx_precision VALUES(?,?,?)',(tid,tx['precision_version'],canonical(tx.get('precision_errors',[]))))
    def close(self):self.db.close()
    def dead_slot(self,slot):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO stream_dead_slots VALUES(?)',(slot,))
            self.db.execute("UPDATE blocks SET state='dead_branch' WHERE slot=? AND json_extract(receipt,'$.source')='ticking_retained'",(slot,))
    def ingest(self,block,receipt):
        h=receipt['car_sha256'];slot=block['slot']
        existing=self.db.execute('SELECT id FROM blocks WHERE slot=? AND hash=?',(slot,h)).fetchone()
        if existing:
            with self.db:
                for t in block['transactions']:
                    row=self.db.execute('SELECT id FROM transactions WHERE block=? AND signature=?',(existing[0],decode(t['signature']))).fetchone()
                    if row:self.enrich(row[0],t)
            return False
        with self.db:
            state=receipt.get('coverage_state','partial_decode' if block['decode_errors'] else 'decoded')
            if receipt.get('source')=='ticking_retained' and self.db.execute('SELECT 1 FROM stream_dead_slots WHERE slot=?',(slot,)).fetchone():state='dead_branch'
            bid=self.db.execute('INSERT INTO blocks(slot,stamp,hash,state,tx_nodes,decoded,errors,receipt) VALUES(?,?,?,?,?,?,?,?)',
                (slot,block['time'],h,state,block['transaction_nodes'],
                 len(block['transactions']),canonical(block['decode_errors']),canonical({k:v for k,v in receipt.items() if k!='raw'}))).lastrowid
            keys={decode(k) for t in block['transactions'] for k in t['account_keys']}
            self.db.executemany('INSERT OR IGNORE INTO keys(key) VALUES(?)',((k,) for k in keys))
            lookup={}
            values=list(keys)
            for i in range(0,len(values),500):
                part=values[i:i+500]
                for r in self.db.execute('SELECT id,key FROM keys WHERE key IN ('+','.join('?' for _ in part)+')',part):lookup[encode(r['key'])]=r['id']
            for t in block['transactions']:
                tid=self.db.execute('INSERT INTO transactions(block,signature,slot,tx_order,stamp,successful) VALUES(?,?,?,?,?,?)',
                    (bid,decode(t['signature']),slot,t['index'],block['time'],int(t['success']))).lastrowid
                self.db.executemany('INSERT INTO appearances VALUES(?,?,?)',((lookup[k],slot,tid) for k in set(t['account_keys'])))
                self.enrich(tid,t)
                if not t['success']:continue
                for e in t['transfers']:
                    self.db.execute('INSERT INTO transfers(tx,ix,source,destination,mint,amount,kind,authority) VALUES(?,?,?,?,?,?,?,?)',
                        (tid,e['ix'],lookup[e['source']],lookup[e['destination']],e['mint'],e['amount'],e['kind'],e.get('authority','')))
                for e in t['activity']:
                    self.db.execute('INSERT OR IGNORE INTO activity(tx,ix,wallet,mint,action) VALUES(?,?,?,?,?)',
                        (tid,e['ix'],lookup[e['wallet']],e['mint'],e['action']))
                for e in t['launches']:
                    for who in {e['creator'],e['payer']}:
                        if who in lookup:self.db.execute('INSERT OR IGNORE INTO activity(tx,ix,wallet,mint,action) VALUES(?,?,?,?,?)',
                            (tid,e['ix'],lookup[who],e['mint'],'create' if who==e['creator'] else 'launch_payer'))
        return True


def read_indexed_slot(reader,slot,index,guard):
    if type(slot) is not int or not 0<=slot<=1000000000:raise ValueError('INDEX_SLOT_BOUND')
    guard();epoch=slot//432000;offset=(slot%432000)*12
    ix=reader.request(f'{BASE}/{epoch}/epoch-{epoch}-slot-ranges.raw',offset,offset+11)
    pos,size=struct.unpack('<QI',ix)
    if not size:return None,None  # Provider absence is not a proven skipped slot.
    raw=reader.request(f'{BASE}/{epoch}/epoch-{epoch}.car',pos,pos+size-1)
    block=parse_index_block(raw,slot,reader.meta_type)
    receipt=dict(index=ix.hex(),car_offset=pos,car_size=size,car_sha256=hashlib.sha256(raw).hexdigest(),
        content_hashes_verified=True,transaction_signatures_verified=True,independent_consensus_verified=False)
    guard();index.ingest(block,receipt)
    return block,dict(receipt,raw=raw)


def search(path,wallet,before_slot=1000000001,before_id=2**63-1,limit=50):
    if not address(wallet) or type(limit) is not int or not 1<=limit<=100:raise ValueError('INDEX_SEARCH_ARGUMENT')
    started=time.monotonic()
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=1)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda:int(time.monotonic()-started>2),10000)
        rows=db.execute('''WITH matched(tx) AS (
              SELECT a.tx FROM keys k JOIN appearances a ON a.account=k.id WHERE k.key=?
              UNION SELECT tx FROM token_owners WHERE owner=?
            ) SELECT t.*,b.hash,b.state,b.receipt,
              EXISTS(SELECT 1 FROM appearances a JOIN keys k ON k.id=a.account
                     WHERE a.tx=t.id AND k.key=?) AS matched_account_key
            FROM matched m JOIN transactions t ON t.id=m.tx JOIN blocks b ON b.id=t.block
            WHERE b.state!='dead_branch' AND (t.slot,t.id)<(?,?) ORDER BY t.slot DESC,t.id DESC LIMIT ?''',
            (decode(wallet),wallet,decode(wallet),before_slot,before_id,limit+1)).fetchall()
        result=[]
        for r in rows[:limit]:
            tx=dict(r);tx['signature']=encode(tx['signature']);receipt=json.loads(tx.pop('receipt'))
            tx['source']=receipt.get('source','reconstructed_public_block');tx['source_locator']=receipt.get('locator')
            tx['signature_verification']=receipt.get('transaction_signatures_verified',False)
            tx['independent_consensus_verified']=False
            snapshots=[dict(e) for e in db.execute('SELECT account,phase,owner,mint,amount,decimals FROM token_owners WHERE tx=?',(r['id'],))]
            tx['token_owner_snapshots']=snapshots
            tx['address_match']=dict(account_key_present=bool(tx.pop('matched_account_key')),
                token_owner_boundary_observations=[s for s in snapshots if s['owner']==wallet],
                instruction_time_owner_proven=False,same_person_proven=False)
            precision=db.execute('SELECT errors FROM tx_precision WHERE tx=?',(r['id'],)).fetchone()
            tx['precision_metadata_present']=bool(precision)
            tx['precision_errors']=json.loads(precision[0]) if precision else None
            commit=db.execute('SELECT body FROM tx_commit_evidence WHERE tx=? ORDER BY version DESC LIMIT 1',(r['id'],)).fetchone()
            tx['instruction_commit_evidence']=json.loads(commit[0]) if commit else None
            transfer_rows=[dict(e,source=encode(e['source']),destination=encode(e['destination'])) for e in db.execute('''
                SELECT f.ix,s.key AS source,d.key AS destination,f.mint,f.amount,f.kind,f.authority
                FROM transfers f JOIN keys s ON s.id=f.source JOIN keys d ON d.id=f.destination WHERE tx=? ORDER BY ix''',(r['id'],))]
            tx['transfers']=[];tx['transfer_commit_omissions']=dict(rejected=0,legacy_unchecked=0)
            for e in transfer_rows:
                status=transfer_commit_status(e['ix'],tx['instruction_commit_evidence']) if tx['successful'] else 'rejected'
                if status!='committed':
                    tx['transfer_commit_omissions'][status]+=1;continue
                e['instruction_commit_status']=status
                if e['mint']!='SOL':
                    e['source_owner_evidence']=owner_evidence(snapshots,e['source'],e['mint'])
                    e['destination_owner_evidence']=owner_evidence(snapshots,e['destination'],e['mint'])
                tx['transfers'].append(e)
            result.append(tx)
        cursor=dict(before_slot=rows[limit-1]['slot'],before_id=rows[limit-1]['id']) if len(rows)>limit else None
        coverage=[dict(r) for r in db.execute('SELECT state,count(*) blocks,min(slot) first_slot,max(slot) last_slot FROM blocks GROUP BY state')]
    return dict(address=wallet,transactions=result,next_cursor=cursor,coverage=coverage,
        complete_address_history=False,unread_ranges_remain=True,absent_means_no_match_in_index_only=True,
        search_ms=(time.monotonic()-started)*1000)
