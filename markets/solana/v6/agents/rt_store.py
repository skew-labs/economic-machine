"""Shared durable limits; independent actors can each have one pending transaction."""
import json,sqlite3,time
TERMINAL={'CONFIRMED','FAILED','EXPIRED','CANCELLED'}
class Store:
    def __init__(self,path,mandate):
        self.db=sqlite3.connect(path,timeout=10);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS mandate(id INTEGER PRIMARY KEY,body TEXT); CREATE TABLE IF NOT EXISTS tx(id INTEGER PRIMARY KEY,role INTEGER,signature TEXT UNIQUE,state TEXT,raw TEXT,body TEXT,receipt TEXT); CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY,role INTEGER,kind TEXT,unix_ns INTEGER,body TEXT);')
        self.db.executescript("CREATE INDEX IF NOT EXISTS tx_role_state ON tx(role,state,id); CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_role ON tx(role) WHERE state NOT IN ('CONFIRMED','FAILED','EXPIRED','CANCELLED');")
        self.db.executescript("CREATE INDEX IF NOT EXISTS tx_role_opcode ON tx(role,json_extract(body,'$.op')); CREATE INDEX IF NOT EXISTS event_role_kind ON event(role,kind);")
        encoded=json.dumps(mandate,sort_keys=True);row=self.db.execute('SELECT body FROM mandate WHERE id=1').fetchone()
        if row and row['body']!=encoded:raise RuntimeError('immutable operating mandate mismatch')
        self.db.execute('INSERT OR IGNORE INTO mandate VALUES(1,?)',(encoded,));self.db.commit();self.mandate=mandate
    def rows(self,role=None):
        return [dict(r) for r in self.db.execute('SELECT * FROM tx'+(' WHERE role=?' if role is not None else '')+' ORDER BY id',() if role is None else (role,))]
    def get(self,signature):
        r=self.db.execute('SELECT * FROM tx WHERE signature=?',(signature,)).fetchone()
        return dict(r) if r else None
    def count_op(self,role,op):return self.db.execute("SELECT COUNT(*) FROM tx WHERE role=? AND json_extract(body,'$.op')=?",(role,op)).fetchone()[0]
    def pending(self,role):return [dict(r) for r in self.db.execute("SELECT * FROM tx WHERE role=? AND state NOT IN ('CONFIRMED','FAILED','EXPIRED','CANCELLED') ORDER BY id",(role,))]
    def count(self,role=None):return self.db.execute('SELECT COUNT(*) FROM tx'+(' WHERE role=?' if role is not None else ''),() if role is None else (role,)).fetchone()[0]
    def last_confirmed(self,role):
        r=self.db.execute("SELECT body FROM tx WHERE role=? AND state='CONFIRMED' ORDER BY id DESC LIMIT 1",(role,)).fetchone()
        return json.loads(r[0]) if r else None
    def event(self,role,kind,body):
        with self.db:self.db.execute('INSERT INTO event(role,kind,unix_ns,body) VALUES(?,?,?,?)',(role,kind,time.time_ns(),json.dumps(body,separators=(',',':'))))
    def count_events(self,role,kind):return self.db.execute('SELECT COUNT(*) FROM event WHERE role=? AND kind=?',(role,kind)).fetchone()[0]
    def prepare(self,role,signature,raw,body):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.pending(role):raise RuntimeError('actor has unresolved transaction')
            if self.count()>=self.mandate['max_transactions'] or self.count(role)>=self.mandate['actor_transaction_limit']:raise RuntimeError('persistent transaction limit')
            if time.time()>=self.mandate['ends_unix']:raise RuntimeError('operating mandate expired')
            self.db.execute('INSERT INTO tx(role,signature,state,raw,body) VALUES(?,?,?,?,?)',(role,signature,'PENDING',raw,json.dumps(body)))
    def update(self,signature,state,body,receipt=None):
        with self.db:self.db.execute('UPDATE tx SET state=?,body=?,receipt=? WHERE signature=?',(state,json.dumps(body),None if receipt is None else json.dumps(receipt),signature))
    def note_body(self,signature,body):
        with self.db:self.db.execute('UPDATE tx SET body=? WHERE signature=?',(json.dumps(body),signature))
