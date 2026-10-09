"""Single-writer durable reservations. UNKNOWN keeps its entire reservation."""
import json,sqlite3
class Journal:
    def __init__(self,path,mandate):
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS mandate (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS tx (label TEXT PRIMARY KEY, signature TEXT UNIQUE NOT NULL, raw TEXT NOT NULL, state TEXT NOT NULL, reservation INTEGER NOT NULL, actual INTEGER, context TEXT NOT NULL, receipt TEXT)')
        body=json.dumps(mandate,sort_keys=True)
        row=self.db.execute('SELECT body FROM mandate WHERE id=1').fetchone()
        if row and row[0]!=body:raise RuntimeError('mandate changed across restart')
        self.db.execute('INSERT OR IGNORE INTO mandate VALUES (1,?)',(body,));self.db.commit();self.mandate=mandate
    def rows(self):
        self.db.row_factory=sqlite3.Row
        return [dict(r) for r in self.db.execute('SELECT * FROM tx ORDER BY rowid')]
    def get(self,label):return next((r for r in self.rows() if r['label']==label),None)
    def prepare(self,label,signature,raw,reservation,context):
        if type(reservation) is not int or reservation<0:raise RuntimeError('invalid reservation')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows=self.rows()
            if any(r['state']=='PREPARED' for r in rows):raise RuntimeError('UNKNOWN transaction blocks new exposure')
            if len(rows)>=self.mandate['max_transactions']:raise RuntimeError('transaction bound reached')
            held=sum(r['actual'] if r['actual'] is not None else r['reservation'] for r in rows)
            if held+reservation>self.mandate['gross_turnover_limit']:raise RuntimeError('gross turnover bound reached')
            self.db.execute('INSERT INTO tx VALUES (?,?,?,?,?,NULL,?,NULL)',(label,signature,raw,'PREPARED',reservation,json.dumps(context,sort_keys=True)))
    def confirm(self,label,receipt):
        row=self.get(label)
        if receipt is None or receipt['meta']['err'] is not None:raise RuntimeError('successful receipt required')
        if receipt['transaction']['signatures'][0]!=row['signature']:raise RuntimeError('receipt signature mismatch')
        context=json.loads(row['context']);actual=0
        if row['reservation']:
            import base64,struct
            result=receipt['meta'].get('returnData')
            if not result or result['programId']!=context['program']:raise RuntimeError('matching turnover receipt required')
            blob=base64.b64decode(result['data'][0],validate=True)
            if len(blob)!=16:raise RuntimeError('malformed turnover receipt')
            filled,turnover=struct.unpack('<QQ',blob);actual=2*turnover
            if filled!=context['lots'] or actual>row['reservation']:raise RuntimeError('fill or turnover outside reservation')
        with self.db:
            self.db.execute('UPDATE tx SET state=?,actual=?,receipt=? WHERE label=?',('CONFIRMED',actual,json.dumps(receipt),label))
    def retire_unlanded_setup(self,label,proof):
        row=self.get(label)
        if not label.startswith('setup-agent-') or row['state']!='PREPARED' or row['reservation']!=0:
            raise RuntimeError('only an unlanded account setup can be retired')
        context=json.loads(row['context'])
        if proof['finalized_height']<=context['last_valid_block_height'] or not proof['token_absent'] or not proof['seat_absent']:
            raise RuntimeError('expiry and absent account proof required')
        context['unlanded_proof']=proof
        with self.db:
            self.db.execute('UPDATE tx SET label=?,state=?,actual=0,context=? WHERE label=?',
                (label+'-expired-'+str(len(self.rows())),'EXPIRED_UNLANDED',json.dumps(context),label))
    def retire_failed_revocation(self,label,receipt,proof):
        row=self.get(label)
        if label not in ('revoke-0','revoke-1','revoke-2') or row['state']!='PREPARED' or row['reservation']!=0:raise RuntimeError('only failed cleanup revocation')
        if receipt['transaction']['signatures'][0]!=row['signature'] or receipt['meta']['err']!={'InstructionError':[1,{'Custom':8}]}:raise RuntimeError('revocation failure identity')
        if proof['confirmation_status']!='finalized' or proof['slot']!=receipt['slot'] or proof['expiry_slot']>=receipt['slot']:raise RuntimeError('finalized expiry proof required')
        context=json.loads(row['context']);context['failed_revocation_proof']=proof
        with self.db:self.db.execute('UPDATE tx SET label=?,state=?,actual=0,context=?,receipt=? WHERE label=?',
            (label+'-failed-'+str(len(self.rows())),'FAILED_FINALIZED',json.dumps(context),json.dumps(receipt),label))
    def summary(self):
        rows=self.rows()
        return {'signed_attempts':len(rows),'confirmed_transactions':sum(r['state']=='CONFIRMED' for r in rows),
                'expired_unlanded':sum(r['state']=='EXPIRED_UNLANDED' for r in rows),
                'failed_finalized':sum(r['state']=='FAILED_FINALIZED' for r in rows),
                'gross_turnover':sum(r['actual'] or 0 for r in rows),'unresolved':sum(r['state']=='PREPARED' for r in rows),
                'cu':[{'label':r['label'],'cu':json.loads(r['receipt'])['meta'].get('computeUnitsConsumed')} for r in rows if r['receipt']]}
