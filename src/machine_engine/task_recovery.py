"""Payment recovery observes existing captures; only an owner requests refunds."""
import json
import os
import re
import secrets

from economic_machine.values import MachineError, digest, require_keys
from .paypal import bound_order, provider_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_recovery (
 id TEXT PRIMARY KEY, due INTEGER NOT NULL, lease TEXT, lease_until INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS task_webhooks (
 id TEXT PRIMARY KEY, body_hash TEXT NOT NULL, purchase_id TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_refunds (
 purchase_id TEXT PRIMARY KEY, request_key TEXT NOT NULL, refund_id TEXT);
"""
PENDING = ('AWAITING_APPROVAL','CAPTURING','CAPTURE_UNKNOWN','PAID','CANCEL_REQUESTED','REFUNDING','REFUND_UNKNOWN')


def verified_webhook(provider, headers, event):
    webhook_id = os.environ.get('ENGINE_PAYPAL_WEBHOOK_ID', '')
    if not webhook_id or not re.fullmatch(r'[A-Z0-9-]{8,100}', webhook_id):
        raise MachineError('PAYPAL_WEBHOOK_NOT_CONFIGURED')
    if not isinstance(event, dict) or len(json.dumps(event)) > 48000:
        raise MachineError('BOUNDED_WEBHOOK_REQUIRED')
    eid = event.get('id')
    if not isinstance(eid, str) or not re.fullmatch(r'[A-Z0-9-]{8,100}', eid):
        raise MachineError('PAYPAL_WEBHOOK_ID_REQUIRED')
    # PayPal verifies the signature; no caller-supplied certificate is downloaded.
    provider.verify_webhook(headers, event, webhook_id)
    try:
        order = provider_id(event['resource']['supplementary_data']['related_ids']['order_id'])
    except (KeyError, TypeError):
        raise MachineError('PAYPAL_ORDER_WEBHOOK_REQUIRED') from None
    return order


class TaskRecovery:
    def __init__(self, work):
        self.work, self.checkout = work, work.task_checkout
        with work.runtime.connect() as db:
            db.executescript(SCHEMA)

    def cancel(self, pid, raw):
        require_keys(raw, {'plan_hash'}, 'purchase cancellation')
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.checkout._row(db, pid)
            if raw['plan_hash'] != self.checkout._plan(row)['hash']:
                raise MachineError('FROZEN_APPROVAL_OR_POLICY_INVALID')
            if row['status'] == 'PLANNED': status = 'REVOKED'
            elif row['status'] in {'PAID','DELIVERED'}: status = 'REFUND_REQUIRED'
            elif row['status'] in {'CREATING','CREATE_UNKNOWN'}:
                raise MachineError('UNKNOWN_ORDER_REQUIRES_RECONCILIATION')
            elif row['status'] in {'AWAITING_APPROVAL','CAPTURING','CAPTURE_UNKNOWN'}: status = 'CANCEL_REQUESTED'
            else: return self.checkout._public(row)
            db.execute('UPDATE engine_task_purchases SET status=? WHERE id=?', (status,pid))
            self.checkout._event(db,self.checkout._row(db,pid),'TASK_PURCHASE_CANCEL_REQUESTED')
        return self.checkout.get(pid)

    def reconcile(self, pid):
        row = self.checkout.get(pid)
        if row['status'] in {'REFUNDING','REFUND_UNKNOWN'}:
            capture = self.checkout._provider().read_capture(row['capture_id'])
            if (capture.get('id') != row['capture_id'] or capture.get('amount') !=
                    {'currency_code':'USD','value':row['plan']['amount']}):
                raise MachineError('REFUND_CAPTURE_BINDING_FAILED')
            if capture.get('status') == 'REFUNDED':
                with self.work.runtime.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE engine_task_purchases SET status='REFUNDED' WHERE id=? AND status IN ('REFUNDING','REFUND_UNKNOWN')",(pid,))
                    self.checkout._event(db,self.checkout._row(db,pid),'TASK_REFUND_VERIFIED')
        elif row['status'] == 'CANCEL_REQUESTED':
            provider = self.checkout._provider()
            order = provider.read(row['order_id'])
            bound_order(order,row['plan'],row['plan']['merchant_id'],row['order_id'])
            if order['status'] == 'COMPLETED':
                cid = bound_order(order,row['plan'],row['plan']['merchant_id'],row['order_id'],paid=True)
                with self.work.runtime.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE engine_task_purchases SET status='REFUND_REQUIRED',capture_id=? WHERE id=? AND status='CANCEL_REQUESTED'",(cid,pid))
                    self.checkout._event(db,self.checkout._row(db,pid),'TASK_PAID_CANCEL_REQUIRES_REFUND')
        else:
            self.checkout.reconcile(pid)
        if self.checkout.get(pid)['status'] == 'PAID':
            self.checkout.fulfill(pid)
        return self.checkout.get(pid)

    def refund(self, pid, raw):
        require_keys(raw, {'plan_hash'}, 'owner refund approval')
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.checkout._row(db,pid)
            plan = self.checkout._plan(row)
            if plan['hash'] != raw['plan_hash']: raise MachineError('FROZEN_APPROVAL_OR_POLICY_INVALID')
            if row['status'] in {'REFUNDING','REFUND_UNKNOWN','REFUNDED'}: return self.checkout._public(row)
            if row['status'] != 'REFUND_REQUIRED' or not row['capture_id']:
                raise MachineError('CONFIRMED_CAPTURE_REFUND_REQUIRED')
            key = secrets.token_hex(16)
            db.execute('INSERT INTO task_refunds VALUES (?,?,NULL)',(pid,key))
            db.execute("UPDATE engine_task_purchases SET status='REFUNDING' WHERE id=?",(pid,))
            self.checkout._event(db,self.checkout._row(db,pid),'TASK_REFUND_INTENT')
        try:
            response = self.checkout._provider().refund(row['capture_id'],plan['amount'],key)
            rid = provider_id(response['id'])
        except Exception:
            rid = None
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE task_refunds SET refund_id=? WHERE purchase_id=?',(rid,pid))
            db.execute("UPDATE engine_task_purchases SET status='REFUND_UNKNOWN' WHERE id=? AND status='REFUNDING'",(pid,))
        # A POST response alone never proves a completed refund.
        return self.reconcile(pid)

    def webhook(self, pid, event):
        fingerprint = digest(event)
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.checkout._row(db,pid)
            prior = db.execute('SELECT * FROM task_webhooks WHERE id=?',(event['id'],)).fetchone()
            if prior and (prior['body_hash'] != fingerprint or prior['purchase_id'] != pid):
                raise MachineError('WEBHOOK_REPLAY_CONFLICT')
            if prior and prior['status'] == 'DONE': return {'accepted':True,'duplicate':True}
            db.execute("INSERT OR IGNORE INTO task_webhooks VALUES (?,?,?,'PENDING')",(event['id'],fingerprint,pid))
        row = self.checkout.get(pid)
        if row['capture_id'] and event.get('event_type') in {
                'PAYMENT.CAPTURE.REFUNDED', 'PAYMENT.CAPTURE.REVERSED'}:
            capture = self.checkout._provider().read_capture(row['capture_id'])
            if (capture.get('id') != row['capture_id'] or capture.get('amount') !=
                    {'currency_code':'USD','value':row['plan']['amount']}):
                raise MachineError('REFUND_CAPTURE_BINDING_FAILED')
            status = {'REFUNDED':'REFUNDED','PARTIALLY_REFUNDED':'REFUND_REVIEW',
                      'REVERSED':'PAYMENT_REVERSED'}.get(capture.get('status'))
            if status:
                with self.work.runtime.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('UPDATE engine_task_purchases SET status=? WHERE id=?',(status,pid))
                    self.checkout._event(db,self.checkout._row(db,pid),'TASK_CAPTURE_ENTITLEMENT_REVOKED')
        self.reconcile(pid)
        with self.work.runtime.connect() as db:
            db.execute("UPDATE task_webhooks SET status='DONE' WHERE id=?",(event['id'],))
        return {'accepted':True,'duplicate':False}

    def tick(self):
        now, lease = int(self.work.clock()), secrets.token_hex(16)
        with self.work.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO task_recovery(id,due) SELECT id,? FROM engine_task_purchases WHERE status IN ('+','.join('?' for _ in PENDING)+')',(now,*PENDING))
            row = db.execute('SELECT r.id FROM task_recovery r JOIN engine_task_purchases p ON p.id=r.id WHERE r.due<=? AND r.lease_until<=? AND p.status IN ('+','.join('?' for _ in PENDING)+') ORDER BY r.due,r.id LIMIT 1',(now,now,*PENDING)).fetchone()
            if not row:return 0
            db.execute('UPDATE task_recovery SET lease=?,lease_until=? WHERE id=?',(lease,now+180,row['id']))
        try:
            self.reconcile(row['id']); delay=30
        except Exception:
            delay=120
        with self.work.runtime.connect() as db:
            db.execute('UPDATE task_recovery SET lease=NULL,lease_until=0,due=? WHERE id=? AND lease=?',(int(self.work.clock())+delay,row['id'],lease))
        return 1
