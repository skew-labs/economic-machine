"""Two real, zero external-fee office-data plans; exact owner consent and replay."""
import csv
import io
import json
import secrets
from economic_machine.values import MachineError, canonical, digest, require_keys
from .task_checkout import clean_csv

SCHEMA='''CREATE TABLE IF NOT EXISTS engine_local_work(
 id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
 task_id TEXT NOT NULL, plan TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
 result_hash TEXT, created INTEGER NOT NULL);
'''

class LocalWork:
    def __init__(self,work):
        self.work=work
        with work.runtime.connect() as db:db.executescript(SCHEMA)

    @staticmethod
    def public(row):
        plans=json.loads(row['plan'])
        for p in plans:
            if p['hash']!=digest({k:v for k,v in p.items() if k!='hash'}):raise MachineError('LOCAL_PLAN_INTEGRITY_FAILED')
        return {'id':row['id'],'task_id':row['task_id'],'status':row['status'],
            'plans':[{k:v for k,v in p.items() if k!='input'} for p in plans],
            'result_hash':row['result_hash'],'external_charge_cents':0,'external_payment':False}

    def plans(self,tid,raw):
        require_keys(raw,{'request_id','expected_revision','input'},'local task plans')
        if not isinstance(raw['request_id'],str) or not 1<=len(raw['request_id'])<=100:raise MachineError('REQUEST_ID_REQUIRED')
        fingerprint=digest({'task_id':tid,'raw':raw})
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM engine_local_work WHERE request_id=?',(raw['request_id'],)).fetchone()
            if old:
                if old['request_hash']!=fingerprint:raise MachineError('REQUEST_ID_CONFLICT')
                return self.public(old)
            if db.execute('SELECT COUNT(*) FROM engine_local_work').fetchone()[0]>=128:raise MachineError('LOCAL_WORK_CAPACITY')
            task=self.work.tasks._public(db,self.work.tasks._row(db,tid))
            if task['status']!='READY_TO_PLAN' or type(raw['expected_revision']) is not int or task['revision']!=raw['expected_revision']:raise MachineError('FRESH_COMPLETE_TASK_REQUIRED')
            if task['brief']['kind']!='data_cleanup':raise MachineError('LOCAL_DATA_CLEANUP_ONLY')
            cond=task['brief']['constraints']
            clean_csv(raw['input'],cond['required_fields'],cond['output_format'])
            now=int(self.work.clock());lid='local-'+secrets.token_hex(12)
            plans=[]
            for method,title in [('trim','Clean spacing, keep every row'),('deduplicate','Clean spacing and remove exact duplicate rows')]:
                p={'id':lid,'task_id':tid,'revision':task['revision'],'brief_hash':task['brief_hash'],
                   'method':method,'title':title,'input':raw['input'],'input_hash':digest(raw['input']),
                   'columns':cond['required_fields'],'format':cond['output_format'],
                   'price_cents':0,'currency':'USD','cost_scope':'NO_EXTERNAL_SERVICE_FEE_LOCAL_COMPUTE_NOT_PRICED',
                   'expires_at':min(now+900,task['brief']['deadline_at'] or now+900)}
                p['hash']=digest(p);plans.append(p)
            db.execute("INSERT INTO engine_local_work VALUES(?,?,?,?,?,'AWAITING_APPROVAL',NULL,NULL,?)",(lid,raw['request_id'],fingerprint,tid,canonical(plans).decode(),now))
            self.work.event(db,'LOCAL_WORK_PLANS',{'id':lid,'task_id':tid,'plan_hashes':[p['hash'] for p in plans]})
            return self.public(db.execute('SELECT * FROM engine_local_work WHERE id=?',(lid,)).fetchone())

    def run(self,lid,raw):
        require_keys(raw,{'plan_hash'},'approved local work')
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM engine_local_work WHERE id=?',(lid,)).fetchone()
            if row is None:raise MachineError('LOCAL_WORK_NOT_FOUND')
            self.public(row)
            plan=next((p for p in json.loads(row['plan']) if p['hash']==raw['plan_hash']),None)
            if plan is None:raise MachineError('REVIEWED_PLAN_REQUIRED')
            if row['status']=='DELIVERED':
                result=json.loads(row['result'])
                if result['plan_hash']!=plan['hash']:raise MachineError('DIFFERENT_PLAN_ALREADY_EXECUTED')
                return self.public(row)
            task=self.work.tasks._public(db,self.work.tasks._row(db,row['task_id']))
            if (task['status']!='READY_TO_PLAN' or task['revision']!=plan['revision'] or task['brief_hash']!=plan['brief_hash'] or int(self.work.clock())>=plan['expires_at']):raise MachineError('LOCAL_PLAN_EXPIRED_OR_REVISED')
            if db.execute("SELECT 1 FROM engine_task_purchases WHERE task_id=? AND status NOT IN ('REVOKED')",(row['task_id'],)).fetchone():raise MachineError('TASK_ALREADY_HAS_PURCHASE')
            source=plan['input'];before=len(list(csv.reader(io.StringIO(source['csv']))))-1
            if plan['method']=='deduplicate':
                rows=list(csv.reader(io.StringIO(source['csv']),strict=True))
                unique=list(dict.fromkeys(tuple(cell.strip() for cell in row) for row in rows[1:]))
                stream=io.StringIO();writer=csv.writer(stream);writer.writerows([rows[0],*unique]);source={'csv':stream.getvalue()}
            result=clean_csv(source,plan['columns'],plan['format'])
            result.update(plan_hash=plan['hash'],input_hash=plan['input_hash'],method=plan['method'],
                          source_rows=before,removed_rows=before-result['rows'],external_charge_cents=0)
            fingerprint=digest(result)
            db.execute("UPDATE engine_local_work SET status='DELIVERED',result=?,result_hash=? WHERE id=?",(canonical(result).decode(),fingerprint,lid))
            self.work.event(db,'LOCAL_WORK_DELIVERED',{'id':lid,'task_id':row['task_id'],'result_hash':fingerprint,'external_charge_cents':0})
            return self.public(db.execute('SELECT * FROM engine_local_work WHERE id=?',(lid,)).fetchone())

    def status(self):
        with self.work.runtime.connect() as db:return {'jobs':[self.public(r) for r in db.execute('SELECT * FROM engine_local_work ORDER BY created DESC,rowid DESC LIMIT 128')]}

    def result(self,lid):
        with self.work.runtime.connect() as db:row=db.execute('SELECT * FROM engine_local_work WHERE id=?',(lid,)).fetchone()
        if row is None or row['status']!='DELIVERED':raise MachineError('LOCAL_RESULT_NOT_DELIVERED')
        result=json.loads(row['result'])
        if digest(result)!=row['result_hash']:raise MachineError('LOCAL_RESULT_INTEGRITY_FAILED')
        return {**result,'result_hash':row['result_hash']}
