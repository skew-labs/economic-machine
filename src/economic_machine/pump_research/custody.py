"""Keep custody gaps and unattributed follow-up branches durable.

A public exchange label is an attribution claim, not an ownership proof at a
historical time. No amount/time match resolves an exchange's private ledger.
Public bridge leads live separately; they do not resolve these custody gaps.
"""
import hashlib
import json
import time

from .store import address, canonical

TRANSFERS = {'funding_record', 'sol_transfer', 'indexed_transfer'}


class Custody:
    def __init__(self, graph):
        self.g = graph
        self.active = set()
        self.labels = {}
        self.digest = None
        self.g.db.executescript('''
          CREATE TABLE IF NOT EXISTS custody_cases(
            id TEXT PRIMARY KEY,root TEXT,entry_edge TEXT UNIQUE,account TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS custody_jobs(
            job TEXT,case_id TEXT,PRIMARY KEY(job,case_id)) WITHOUT ROWID;
          CREATE TABLE IF NOT EXISTS custody_annotations(
            edge TEXT,case_id TEXT,relation TEXT,PRIMARY KEY(edge,case_id)) WITHOUT ROWID;
        ''')
        self.reload()

    def reload(self):
        path = self.g.out/'custody-labels.json'
        if not path.exists():return
        raw = path.read_bytes()
        if len(raw)>2*1024**2:raise ValueError('CUSTODY_LABEL_SIZE')
        digest = hashlib.sha256(raw).hexdigest()
        if digest == self.digest:return
        data = json.loads(raw)
        if data.get('schema')!='economic-machine.custody-label-claims.v1':
            raise ValueError('CUSTODY_LABEL_SCHEMA')
        labels = {}
        for row in data['labels']:
            if (row.get('chain')!='solana' or not address(row.get('address')) or
                not isinstance(row.get('entity'),str) or not row['entity'].strip() or
                row.get('evidence_type')!='third_party_label_claim' or
                not isinstance(row.get('source_sha256'),str) or len(row['source_sha256'])!=64 or
                not str(row.get('source_url','')).startswith('https://')):
                raise ValueError('CUSTODY_LABEL_ROW')
            labels.setdefault(row['address'],[]).append(row)
        self.labels, self.digest = labels, digest
        # New label evidence gets a new derived scan, never a reset of source IO
        # ledgers, original anchors, case IDs or already-completed source pages.
        if self.g.state('custody_label_revision')!=digest:
            with self.g.db:
                self.g.set('custody_scan_row',0)
                self.g.set('custody_label_revision',digest)

    def attach(self, job, cases=None):
        if job:
            self.g.db.executemany('INSERT OR IGNORE INTO custody_jobs VALUES(?,?)',
                ((job,c) for c in (self.active if cases is None else cases)))
        return job

    def inherit(self, parent, child):
        return self.attach(child,[r[0] for r in self.g.db.execute(
            'SELECT case_id FROM custody_jobs WHERE job=?',(parent,))])

    def enter(self, job):
        rows=self.g.db.execute('''SELECT c.id,e.body FROM custody_jobs j JOIN custody_cases c ON c.id=j.case_id
            JOIN edges e ON e.id=c.entry_edge WHERE j.job=?''',(job,)).fetchall()
        self.active = {r[0] for r in rows if self.valid_case(r[0])}
        return not rows or bool(self.active)

    def leave(self):self.active = set()

    def valid_case(self, case, seen=None):
        seen=set() if seen is None else seen
        if case in seen:return False
        row=self.g.db.execute('''SELECT c.body,e.body FROM custody_cases c JOIN edges e ON e.id=c.entry_edge
            WHERE c.id=?''',(case,)).fetchone()
        if not row or json.loads(row[1]).get('dead_branch'):return False
        return all(self.valid_case(parent,seen|{case}) for parent in json.loads(row[0]).get('parent_case_ids',[]))

    def observe(self, ident, root, src, dst, kind, slot, stamp, body):
        if body.get('dead_branch') or body.get('invalid_address'):return
        # Only a concrete observed edge is attached. There is never an edge
        # joining separate hot wallets merely because labels name one exchange.
        for case in self.active:
            self.g.db.execute('INSERT OR IGNORE INTO custody_annotations VALUES(?,?,?)',
                (ident,case,'unattributed_followup'))
        if kind not in TRANSFERS:return
        match_account=dst;label_basis='address_claim'
        claims = self.labels.get(dst,[])
        owner=body.get('destination_owner_evidence',{})
        if not claims and owner.get('status')=='same_owner_at_both_boundaries' and owner.get('boundary_owner') in self.labels:
            match_account=owner['boundary_owner'];claims=self.labels[match_account];label_basis='token_boundary_owner_hint'
        if not claims and not body.get('destination_labeled'):return
        identity=[root,body['signature'],body['ix'],src,dst,slot] if body.get('signature') and body.get('ix') is not None else [root,ident]
        case = hashlib.sha256(canonical(['custody',identity]).encode()).hexdigest()[:24]
        if self.g.db.execute('SELECT 1 FROM custody_cases WHERE id=?',(case,)).fetchone():
            self.g.db.execute('INSERT OR IGNORE INTO custody_annotations VALUES(?,?,?)',(ident,case,'custody_boundary'))
            return
        entities = sorted({r['entity'] for r in claims})
        # Conflicting / missing labels never become an assumed exchange identity.
        entity = entities[0] if len(entities)==1 else None
        for current in self.active:
            old=self.g.db.execute('SELECT account,body FROM custody_cases WHERE id=?',(current,)).fetchone()
            if old and (old[0]==dst or (entity and json.loads(old[1]).get('entity_claim')==entity)):
                return  # Keep the observed cycle; do not multiply pooled-wallet cases.
        value = dict(chain='solana',entity_claim=entity,label_claims=claims,
            label_match_account=match_account,label_match_basis=label_basis,instruction_time_owner_proven=False,
            classification='exchange_label_claim' if entity else 'unresolved_labeled_account',
            entry_source=src,entry_slot=slot,entry_stamp=stamp,entry_signature=body.get('signature'),
            source_tier='external_dataset' if kind=='funding_record' else body.get('source',body.get('finality')),
            parent_case_ids=sorted(self.active),status='OPEN_CUSTODY_GAP',
            historical_label_validity_verified=False,customer_credit_verified=False,
            same_funds_continuity_proven=False,internal_ledger_available=False,
            label_revision=self.digest,created_ms=time.time_ns()//1000000)
        self.g.db.execute('INSERT INTO custody_cases VALUES(?,?,?,?,?)',
            (case,root,ident,dst,canonical(value)))
        self.g.db.execute('INSERT OR IGNORE INTO custody_annotations VALUES(?,?,?)',
            (ident,case,'custody_boundary'))
        wallets = {dst}
        if entity:
            wallets.update(w for w,rs in self.labels.items() if {r['entity'] for r in rs}=={entity})
        contexts = self.active | {case}
        for wallet in sorted(wallets):
            self.g.node(wallet,'account','exchange_label_claim' if entity else 'unresolved_labeled_account')
            # Timestamp/slot anchors prevent selecting pre-deposit observations.
            # Other exchange wallets are a disjoint candidate branch, not a
            # proven transfer of the deposit, even if amounts happen to match.
            if slot is not None or stamp is not None:
                order=(body['tx_order'],body['ix']) if slot is not None and body.get('tx_order') is not None and body.get('ix') is not None else None
                j=self.g.enqueue(root,wallet,slot,stamp,1,'indexed_forward',after_order=order)
                self.attach(j,contexts)
            if stamp is not None:
                j=self.g.enqueue(root,wallet,None,stamp,1,'core_forward')
                self.attach(j,contexts)
        # Existing descendants are not reclassified as owners. A separate
        # anchored investigation branch re-reads retained sources with gap IDs.

    def backfill(self, guard=lambda:None, limit=2000):
        self.reload();guard()
        last=self.g.state('custody_scan_row',0)
        rows=self.g.db.execute('SELECT rowid,* FROM edges WHERE rowid>? ORDER BY rowid LIMIT ?',
            (last,limit)).fetchall()
        with self.g.db:
            for r in rows:
                self.observe(r['id'],r['root'],r['source'],r['target'],r['kind'],r['slot'],r['stamp'],json.loads(r['body']))
            if rows:self.g.set('custody_scan_row',rows[-1]['rowid'])
        return len(rows)

    def annotations(self):
        found={}
        for row in self.g.db.execute('SELECT edge,case_id,relation FROM custody_annotations'):
            found.setdefault(row[0],[]).append(dict(case_id=row[1],relation=row[2]))
        return found

    def snapshot(self):
        cases=[]
        for r in self.g.db.execute('SELECT * FROM custody_cases ORDER BY rowid'):
            body=json.loads(r['body'])
            entry=self.g.db.execute('SELECT body FROM edges WHERE id=?',(r['entry_edge'],)).fetchone()
            dead=bool(entry and json.loads(entry[0]).get('dead_branch'))
            counts=dict(self.g.db.execute('''SELECT f.state,count(*) FROM custody_jobs j JOIN frontier f ON f.id=j.job
                WHERE j.case_id=? GROUP BY f.state''',(r['id'],)))
            examples=[]
            for e in self.g.db.execute('''SELECT e.*,a.relation FROM custody_annotations a JOIN edges e ON e.id=a.edge
                WHERE a.case_id=? AND a.relation!='custody_boundary'
                AND coalesce(json_extract(e.body,'$.dead_branch'),0)=0
                ORDER BY e.rowid DESC LIMIT 8''',(r['id'],)):
                examples.append(dict(edge=e['id'],source=e['source'],target=e['target'],kind=e['kind'],slot=e['slot'],
                    signature=json.loads(e['body']).get('signature'),same_funds_continuity_proven=False))
            cases.append(dict(id=r['id'],root=r['root'],entry_edge=r['entry_edge'],account=r['account'],
                **body,entry_dead_branch=dead,source_job_states=counts,followup_examples=examples))
        return dict(schema='economic-machine.custody-gaps.v1',at_ms=time.time_ns()//1000000,
            cases=cases,summary=dict(cases=len(cases),dead_entries=sum(c['entry_dead_branch'] for c in cases),
                exchange_label_cases=sum(c['entity_claim'] is not None for c in cases),
                unresolved_label_cases=sum(c['entity_claim'] is None for c in cases),
                candidate_followup_records=self.g.db.execute("SELECT count(*) FROM custody_annotations WHERE relation='unattributed_followup'").fetchone()[0],
                label_addresses=len(self.labels),label_entities=len({r['entity'] for rows in self.labels.values() for r in rows}),
                scanned_graph_row=self.g.state('custody_scan_row',0),
                graph_last_row=self.g.db.execute('SELECT coalesce(max(rowid),0) FROM edges').fetchone()[0]),
            scope=dict(connected_chains=['solana'],cex_internal_ledger_available=False,
                cross_chain_reentry_verified=False,all_wallets_covered=False,complete_to_today=False,
                same_funds_continuity_proven=False,orders_submitted=0),
            gaps=['CEX internal transfers and trades have no public customer-level ledger.',
                'Public cross-chain leads, if present, live in crosschain.json and do not resolve private ledger gaps.',
                'Label publication date is not a historical custody validity interval.',
                'Amount and time similarity cannot resolve any custody gap.'])
