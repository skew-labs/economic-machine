"""Append-only lineage, cost reservations and per-user champion compare-and-swap."""
import json,sqlite3,time
from schema import canonical,digest,seed,validate_profile,validate_program,validate_meta
class Archive:
    def __init__(self,path):
        self.db=sqlite3.connect(path);self.db.row_factory=sqlite3.Row;self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS profiles(hash TEXT PRIMARY KEY,body TEXT,champion TEXT,cursor INTEGER); CREATE TABLE IF NOT EXISTS programs(hash TEXT PRIMARY KEY,body TEXT); CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,profile TEXT,parent TEXT,created REAL,cutoff INTEGER,reserved_cost REAL,status TEXT,body TEXT); CREATE TABLE IF NOT EXISTS generations(id INTEGER PRIMARY KEY,attempt INTEGER UNIQUE,profile TEXT,parent TEXT,candidate TEXT,meta TEXT,report TEXT,accepted INTEGER);')
    def initialize(self,p):
        validate_profile(p);s=seed(p);h=digest(s)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO programs VALUES(?,?)',(h,canonical(s)))
            self.db.execute('INSERT OR IGNORE INTO profiles VALUES(?,?,?,0)',(p['profile_hash'],canonical(p),h))
    def profile(self,h):return dict(self.db.execute('SELECT * FROM profiles WHERE hash=?',(h,)).fetchone())
    def program(self,h):return json.loads(self.db.execute('SELECT body FROM programs WHERE hash=?',(h,)).fetchone()[0])
    def history(self,h):return [dict(r) for r in self.db.execute('SELECT * FROM generations WHERE profile=? ORDER BY id',(h,))]
    def reserve(self,h,parent,cutoff):
        cost=16000*0.10/1e6+4096*0.20/1e6
        self.db.execute('BEGIN IMMEDIATE')
        try:
            n,total=self.db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_cost),0) FROM attempts').fetchone()
            if n>=12 or total+cost>0.03:raise ValueError('model budget exhausted')
            current=self.profile(h)
            if cutoff<=current['cursor']:raise ValueError('reused holdout')
            r=self.db.execute('INSERT INTO attempts(profile,parent,created,cutoff,reserved_cost,status,body) VALUES(?,?,?,?,?,?,?)',(h,parent,time.time(),cutoff,cost,'RESERVED','{}'))
            self.db.commit();return r.lastrowid
        except: self.db.rollback();raise
    def result(self,i,status,body):
        with self.db:self.db.execute('UPDATE attempts SET status=?,body=? WHERE id=?',(status,canonical(body),i))
    def commit(self,i,candidate,meta,report):
        validate_program(candidate);validate_meta(meta);h=digest(candidate);row=self.db.execute('SELECT * FROM attempts WHERE id=?',(i,)).fetchone()
        window=report['window'];accepted=report['research_champion_eligible']
        if window['first_id']<=row['cutoff'] or window['first_unix']<=row['created']:raise ValueError('holdout existed before proposal')
        proposal_record=json.loads(row['body'])
        if row['status']!='PROPOSED' or window['first_id']<=proposal_record.get('holdout_after_id',window['first_id']) or window['first_unix']<=proposal_record.get('finished_unix',window['first_unix']):raise ValueError('holdout is not post-proposal')
        with self.db:
            state=self.profile(row['profile'])
            if window['first_id']<=state['cursor']:raise ValueError('holdout reused')
            self.db.execute('INSERT OR IGNORE INTO programs VALUES(?,?)',(h,canonical(candidate)))
            # Candidate must beat both its selected parent and current champion.
            if accepted and report.get('champion_hash')!=state['champion']:raise ValueError('champion changed during evaluation')
            self.db.execute('INSERT INTO generations(attempt,profile,parent,candidate,meta,report,accepted) VALUES(?,?,?,?,?,?,?)',(i,row['profile'],row['parent'],h,canonical(meta),canonical(report),accepted))
            self.db.execute('UPDATE profiles SET champion=?,cursor=? WHERE hash=?',(h if accepted else state['champion'],window['last_id'],row['profile']))
            self.db.execute('UPDATE attempts SET status=? WHERE id=?',('EVALUATED',i))
    def summary(self):
        calls,reserved=self.db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_cost),0) FROM attempts').fetchone()
        generations,accepted=self.db.execute('SELECT COUNT(*),COALESCE(SUM(accepted),0) FROM generations').fetchone()
        return {'model_calls_reserved':calls,'reserved_cost_ceiling_usd':round(reserved,7),'generations':generations,'research_champion_changes':accepted,'live_promotions':0}
