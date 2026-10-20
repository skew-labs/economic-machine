import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import tempfile
from decimal import Decimal, InvalidOperation
from . import object_store

PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

def _encode(raw):
    n = int.from_bytes(raw, 'big'); s = ''
    while n:
        n, r = divmod(n, 58); s = ALPHABET[r] + s
    return '1' * (len(raw)-len(raw.lstrip(b'\0'))) + s

def _decode(value):
    n = 0
    for c in value: n = n*58 + ALPHABET.index(c)
    return b'\0'*(len(value)-len(value.lstrip('1'))) + n.to_bytes((n.bit_length()+7)//8, 'big')

# Public account identities repeat across frames. Signatures are high-cardinality
# and deliberately bypass this bounded cache. Never cache credentials or bodies.
_encode_key=lru_cache(maxsize=32768)(_encode)
_decode_key=lru_cache(maxsize=32768)(_decode)

def encode(raw):
    return _encode_key(raw) if isinstance(raw,bytes) and len(raw)==32 else _encode(raw)

def decode(value):
    return _decode_key(value) if isinstance(value,str) and 32<=len(value)<=44 else _decode(value)

def address(value):
    return isinstance(value, str) and 32 <= len(value) <= 44 and all(c in ALPHABET for c in value) and len(decode(value)) == 32

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

def atomic(path, value):
    # Independent writers must never rename or truncate each other's temporary
    # file. The SQLite ledger remains authoritative; this is a status snapshot.
    path = Path(path); body = canonical(value)+'\n'
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.next', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(body); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)

def ath_quality(value):
    try: d = Decimal(str(value))
    except InvalidOperation: return 'invalid'
    if not d.is_finite() or d <= 0: return 'invalid'
    # A triage threshold, never a correctness certificate for values below it.
    return 'quarantined_extreme_value' if d > Decimal('1000000000000') else 'provider_claim_unverified'

class Store:
    def __init__(self, root, quota=5*1024**3, reserve=48*1024**3):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        (self.root/'objects').mkdir(exist_ok=True)
        self.quota, self.reserve = quota, reserve
        self.db = sqlite3.connect(self.root/'search.sqlite3', timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS objects(hash TEXT PRIMARY KEY,bytes INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS archive_objects(hash TEXT PRIMARY KEY REFERENCES objects(hash),root TEXT NOT NULL,
            segment TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,sequence INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,source TEXT,hash TEXT REFERENCES objects(hash),locator TEXT,observed REAL);
        CREATE TABLE IF NOT EXISTS candidates(mint TEXT PRIMARY KEY,name TEXT,symbol TEXT,creator TEXT,created_ms INTEGER,reported_ath TEXT,ath_status TEXT,provider_rank INTEGER,evidence TEXT REFERENCES evidence(id));
        CREATE INDEX IF NOT EXISTS candidate_creator ON candidates(creator);
        CREATE VIRTUAL TABLE IF NOT EXISTS terms USING fts5(mint UNINDEXED,name,symbol,creator,tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,signature TEXT,slot INTEGER,tx_order INTEGER,source TEXT,finality TEXT,successful INTEGER,evidence TEXT REFERENCES evidence(id));
        CREATE INDEX IF NOT EXISTS sig_lookup ON observations(signature,slot);
        CREATE TABLE IF NOT EXISTS transfers(observation TEXT REFERENCES observations(id),ix INTEGER,source TEXT,destination TEXT,mint TEXT,amount TEXT,kind TEXT,authority TEXT,slot INTEGER,PRIMARY KEY(observation,ix));
        CREATE TABLE IF NOT EXISTS launches(observation TEXT REFERENCES observations(id),ix INTEGER,mint TEXT,payer TEXT,creator TEXT,name TEXT,symbol TEXT,slot INTEGER,PRIMARY KEY(observation,ix));
        CREATE INDEX IF NOT EXISTS launches_mint ON launches(mint,slot);
        CREATE INDEX IF NOT EXISTS launches_payer ON launches(payer,slot);
        CREATE TABLE IF NOT EXISTS activity(observation TEXT REFERENCES observations(id),ix INTEGER,mint TEXT,wallet TEXT,action TEXT,slot INTEGER,PRIMARY KEY(observation,ix));
        CREATE INDEX IF NOT EXISTS early_activity ON activity(mint,slot);
        CREATE TABLE IF NOT EXISTS balances(observation TEXT REFERENCES observations(id),wallet TEXT,lamports TEXT,slot INTEGER,PRIMARY KEY(observation,wallet));
        CREATE TABLE IF NOT EXISTS coverage(source TEXT,start INTEGER,end INTEGER,status TEXT,details TEXT,at REAL,PRIMARY KEY(source,start,end));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,args TEXT,status TEXT,attempts INTEGER DEFAULT 0,error TEXT,result TEXT);
        CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,body TEXT,at REAL);
        CREATE TABLE IF NOT EXISTS watchlist(address TEXT PRIMARY KEY,root TEXT,depth INTEGER,reason TEXT,first_slot INTEGER,evidence TEXT);
        CREATE TABLE IF NOT EXISTS launch_hits(observation TEXT,ix INTEGER,mint TEXT,address TEXT,root TEXT,reason TEXT,PRIMARY KEY(observation,ix,address));
        ''')
        if self.db.execute("SELECT type FROM sqlite_master WHERE name='transfers'").fetchone()[0]=='table':
            self.db.execute('CREATE INDEX IF NOT EXISTS inbound ON transfers(destination,slot DESC)')
            self.db.execute('CREATE INDEX IF NOT EXISTS outbound ON transfers(source,slot)')
        if 'stored_bytes' not in {r[1] for r in self.db.execute('PRAGMA table_info(objects)')}:
            self.db.execute('ALTER TABLE objects ADD COLUMN stored_bytes INTEGER')
            self.db.commit()
        if self.db.execute("SELECT type FROM sqlite_master WHERE name='balances'").fetchone()[0]=='table':
            self.db.execute('CREATE INDEX IF NOT EXISTS wallet_balance ON balances(wallet,slot DESC)')
        # Include crash leftovers and duplicate representations, not just DB rows.
        physical=sum(p.stat().st_size for p in (self.root/'objects').iterdir() if p.is_file())
        self._originals=None
        # A concurrent writer may deduplicate objects during startup scanning.
        # Read and publish accounting under the same SQLite writer lock.
        self.db.execute('BEGIN IMMEDIATE')
        with self.db:
            accounted=self.db.execute('SELECT coalesce(sum(coalesce(stored_bytes,bytes)),0) FROM objects').fetchone()[0]
            self.set_state('object_bytes',accounted)
        self.orphan_bytes=max(0,physical-accounted)

    def state(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        self.db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,canonical(value)))

    def guard(self, extra=0):
        # Content-addressed objects are counted transactionally; orphan objects
        # after a crash are reconciled by maintenance, never silently deleted.
        used = self.state('object_bytes')
        if used is None:
            used = self.db.execute('SELECT coalesce(sum(coalesce(stored_bytes,bytes)),0) FROM objects').fetchone()[0]
            self.set_state('object_bytes',used)
        used += self.orphan_bytes + sum(p.stat().st_size for pattern in ('search.sqlite3*','io-budget.sqlite3*') for p in self.root.glob(pattern))
        if used+extra+16*1024**2 > self.quota or shutil.disk_usage(self.root).free-extra < self.reserve:
            raise ValueError('RESEARCH_STORAGE_LIMIT')

    def originals(self,root):
        from .originals import Originals
        from .io_budget import reserve
        root=str(Path(root).resolve())
        if self._originals is None or str(self._originals.root)!=root:
            if self._originals is not None:self._originals.close()
            def reservation(amount,purpose):
                scope=self.root/'research-model-scope.json'
                if scope.exists() and time.time()*1000>=json.loads(scope.read_bytes())['deadline_ms']:
                    raise ValueError('RESEARCH_SCOPE_ENDED')
                return reserve(self,amount,purpose)
            self._originals=Originals(root,reservation)
        return self._originals

    def evidence(self, source, locator, raw, original=None):
        h = hashlib.sha256(raw).hexdigest();plain,packed=object_store.paths(self.root,h)
        ref=self.db.execute('SELECT 1 FROM archive_objects WHERE hash=?',(h,)).fetchone()
        if original is not None:
            root=str(Path(original['root']).resolve());r=original
            if source!='ticking' or locator!=f"{r['segment']}:{r['start']}:{r['sequence']}":raise ValueError('ORIGINAL_REFERENCE_LOCATOR')
            if self.originals(root).read_reference(r,h,len(raw))!=raw:raise ValueError('ORIGINAL_REFERENCE_CONTENT')
            self.guard();size=0
        elif ref:
            if self.read_object(h)!=raw:raise ValueError('OBJECT_HASH_CONFLICT')
            self.guard();size=0
        elif plain.exists() or packed.exists():
            if object_store.read(self.root,h)!=raw:raise ValueError('OBJECT_HASH_CONFLICT')
            size=(packed if packed.exists() else plain).stat().st_size
            self.guard()
        else:
            data,codec=object_store.encode(raw);size=len(data);self.guard(size)
            object_store.write(self.root,h,data,codec)
        ident = hashlib.sha256((source+'\n'+locator+'\n'+h).encode()).hexdigest()
        if self.db.execute('INSERT OR IGNORE INTO objects(hash,bytes,stored_bytes) VALUES(?,?,?)',(h,len(raw),size)).rowcount:
            self.set_state('object_bytes',self.state('object_bytes',0)+size)
            if original is not None:
                self.db.execute('INSERT INTO archive_objects VALUES(?,?,?,?,?,?)',(h,root,r['segment'],r['start'],r['end'],r['sequence']))
        self.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?)',(ident,source,h,locator,time.time()))
        return ident

    def read_object(self,digest):
        r=self.db.execute('SELECT a.*,o.bytes FROM archive_objects a JOIN objects o USING(hash) WHERE hash=?',(digest,)).fetchone()
        if r:return self.originals(r['root']).read_reference(r,digest,r['bytes'])
        return object_store.read(self.root,digest)

    def candidates(self, rows, evidence, offset):
        for rank, c in enumerate(rows, offset+1):
            if not address(c.get('mint')): continue
            mint=c['mint']; creator=c.get('creator') if address(c.get('creator')) else ''
            self.db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET name=excluded.name,symbol=excluded.symbol,creator=excluded.creator,created_ms=excluded.created_ms,reported_ath=excluded.reported_ath,ath_status=excluded.ath_status,provider_rank=min(provider_rank,excluded.provider_rank),evidence=excluded.evidence',
                (mint,str(c.get('name',''))[:256],str(c.get('symbol',''))[:64],creator,c.get('created_timestamp'),str(c.get('ath_market_cap')),ath_quality(c.get('ath_market_cap')),rank,evidence))
            self.db.execute('DELETE FROM terms WHERE mint=?',(mint,))
            self.db.execute('INSERT INTO terms VALUES(?,?,?,?)',(mint,str(c.get('name',''))[:256],str(c.get('symbol',''))[:64],creator))

    def ingest(self, tx, evidence, source, finality):
        if finality not in ('processed_unconfirmed','archive_content_verified','finalized_provider_receipt'):
            raise ValueError('FINALITY_CLASS')
        ident=hashlib.sha256(canonical([tx['signature'],tx['slot'],source,evidence]).encode()).hexdigest()
        added=self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?)',
            (ident,tx['signature'],tx['slot'],tx.get('index',0),source,finality,int(tx['success']),evidence)).rowcount
        if not added or not tx['success']: return 0
        for e in tx['transfers']:
            self.db.execute('INSERT INTO transfers VALUES(?,?,?,?,?,?,?,?,?)',(ident,e['ix'],e['source'],e['destination'],e['mint'],e['amount'],e['kind'],e.get('authority',''),tx['slot']))
        for e in tx['launches']:
            self.db.execute('INSERT INTO launches VALUES(?,?,?,?,?,?,?,?)',(ident,e['ix'],e['mint'],e['payer'],e['creator'],e['name'],e['symbol'],tx['slot']))
            for who in set((e['payer'],e['creator'])):
                watch=self.db.execute('SELECT * FROM watchlist WHERE address=?',(who,)).fetchone()
                if watch:
                    self.db.execute('INSERT OR IGNORE INTO launch_hits VALUES(?,?,?,?,?,?)',(ident,e['ix'],e['mint'],who,watch['root'],'watched_address_launch_association_not_ownership'))
        for e in tx['activity']:
            self.db.execute('INSERT INTO activity VALUES(?,?,?,?,?,?)',(ident,e['ix'],e['mint'],e['wallet'],e['action'],tx['slot']))
        for wallet,lamports in tx.get('balances',[]):
            self.db.execute('INSERT OR IGNORE INTO balances VALUES(?,?,?,?)',(ident,wallet,str(lamports),tx['slot']))
        return 1

    def watch_roots(self):
        self.db.execute("INSERT OR IGNORE INTO watchlist SELECT creator,creator,0,'unverified_provider_creator',0,evidence FROM candidates WHERE creator!=''")
        return {r['address']:dict(r) for r in self.db.execute('SELECT * FROM watchlist')}

    def expand_watch(self, tx, evidence, watches, maximum=5000):
        """Follow public SOL funding associations, without labelling ownership.

        Dust and high-fanout nodes remain searchable but cannot explode live
        subscriptions. Every propagation carries its observed source evidence.
        """
        added=[]
        for e in tx['transfers']:
            if e['mint']!='SOL' or e['kind']!='system_transfer' or int(e['amount'])<10000000: continue
            for parent,child,direction in [(e['source'],e['destination'],'outgoing'),(e['destination'],e['source'],'incoming')]:
                watch=watches.get(parent)
                if not watch or child in watches or watch['depth']>=4 or len(watches)>=maximum: continue
                if self.db.execute('SELECT count(*) FROM (SELECT DISTINCT destination FROM transfers WHERE source=? LIMIT 33)',(parent,)).fetchone()[0]>32: continue
                value=dict(address=child,root=watch['root'],depth=watch['depth']+1,reason=direction+'_funding_association',first_slot=tx['slot'],evidence=evidence)
                if self.db.execute('INSERT OR IGNORE INTO watchlist VALUES(?,?,?,?,?,?)',tuple(value.values())).rowcount:
                    watches[child]=value;added.append(child)
        return added

    def search(self, query, limit=20):
        if not isinstance(query,str) or len(query)>200 or not 1<=limit<=100: raise ValueError('QUERY_BOUND')
        if address(query):
            rows=self.db.execute('SELECT * FROM candidates WHERE mint=? OR creator=? LIMIT ?',(query,query,limit))
        else:
            tokens=re.findall(r'\w+',query,flags=re.UNICODE)[:10]
            if not tokens: return []
            expression=' AND '.join('"'+t.replace('"','""')+'"*' for t in tokens)
            rows=self.db.execute('SELECT c.* FROM terms JOIN candidates c ON c.mint=terms.mint WHERE terms MATCH ? ORDER BY provider_rank LIMIT ?',(expression,limit))
        return [dict(r) for r in rows]

    def trace(self, wallet, depth=3, limit=200, before=2**62, direction='backward', before_order=(2**31,2**31)):
        if not address(wallet) or not 1<=depth<=8 or not 1<=limit<=1000 or direction not in ('backward','forward'): raise ValueError('TRACE_BOUND')
        seen=set(); frontier=[(wallet,(before,*before_order))]; edges=[]; truncated=False
        field='destination' if direction=='backward' else 'source'
        next_field='source' if direction=='backward' else 'destination'
        op='<=' if direction=='backward' else '>='
        for _ in range(depth):
            nxt=[]
            for addr,point in frontier:
                if (addr,point) in seen: continue
                seen.add((addr,point))
                order='DESC' if direction=='backward' else 'ASC'
                rows=list(self.db.execute(f"SELECT t.*,o.signature,o.tx_order,o.finality,o.evidence FROM transfers t JOIN observations o ON o.id=t.observation WHERE o.finality!='dead_branch' AND t.{field}=? AND t.slot {op} ? ORDER BY t.slot {order},o.tx_order {order},t.ix {order} LIMIT ?", (addr,point[0],limit+1)))
                for row in rows:
                    if len(edges)>=limit: truncated=True; break
                    r=dict(row)
                    if int(r['amount'])<=0: continue
                    when=(r['slot'],r['tx_order'],r['ix'])
                    if (direction=='backward' and when>point) or (direction=='forward' and when<point):continue
                    # Same signature/instruction repeated by a replay is one edge.
                    identity=(r['signature'],r['slot'],r['ix'])
                    if identity in seen: continue
                    seen.add(identity);edges.append(r);nxt.append((r[next_field],when))
                if truncated: break
            frontier=nxt
            if truncated or not frontier: break
        balance=self.db.execute("SELECT b.*,o.finality,o.evidence FROM balances b JOIN observations o ON o.id=b.observation WHERE o.finality!='dead_branch' AND wallet=? ORDER BY b.slot DESC LIMIT 1",(wallet,)).fetchone()
        return dict(wallet=wallet,edges=edges,truncated=truncated,depth_limited=bool(frontier),latest_observed_balance=dict(balance) if balance else None,current_balance_verified=False,ownership_inferred=False,complete_history=False,coverage=self.coverage())

    def coverage(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM coverage ORDER BY at DESC LIMIT 30')]

    def token(self, mint):
        if not address(mint): raise ValueError('MINT')
        candidate=self.db.execute('SELECT * FROM candidates WHERE mint=?',(mint,)).fetchone()
        launches=[dict(r) for r in self.db.execute("SELECT l.*,o.signature,o.tx_order,o.finality,o.evidence FROM launches l JOIN observations o ON o.id=l.observation WHERE o.finality!='dead_branch' AND mint=? ORDER BY slot,o.tx_order,ix LIMIT 20",(mint,))]
        early=[dict(r) for r in self.db.execute("SELECT a.*,o.signature,o.tx_order,o.finality,o.evidence FROM activity a JOIN observations o ON o.id=a.observation WHERE o.finality!='dead_branch' AND mint=? ORDER BY slot,o.tx_order,ix LIMIT 100",(mint,))]
        anchors=launches[:1];traces=[]
        for launch in anchors:
            for who in set([launch['payer'],launch['creator']]):
                if address(who): traces.append(self.trace(who,depth=3,limit=80,before=launch['slot'],before_order=(launch['tx_order'],launch['ix'])))
        return dict(mint=mint,candidate=dict(candidate) if candidate else None,observed_launches=launches,earliest_indexed_activity=early,prelaunch_funding_paths=traces,first_ever_trade_verified=False,bundle_membership_verified=False,same_owner_verified=False,ath_verified=False,exit_threshold_recommended=None)

    def summary(self):
        counts={t:self.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('candidates','observations','transfers','launches','activity','evidence','jobs','plans','watchlist','launch_hits')}
        return dict(at=time.time(),counts=counts,ath_quality={r[0]:r[1] for r in self.db.execute('SELECT ath_status,count(*) FROM candidates GROUP BY ath_status')},verified_all_time_top1000=False,complete_wallet_history=False,trading_enabled=False,coverage=self.coverage(),ticking=self.state('ticking'),archive=self.state('archive'))
