import copy
import os
import unittest
from unittest.mock import patch
from test_task_checkout import CheckoutTests
from machine_engine.task_recovery import TaskRecovery, verified_webhook
from economic_machine.values import MachineError


class RecoveryTests(unittest.TestCase):
    setUp = CheckoutTests.setUp
    tearDown = CheckoutTests.tearDown
    approve = CheckoutTests.approve
    pay = CheckoutTests.pay
    def test_paid_job_recovers_after_restart_without_capture(self):
        self.pay()
        recovery=TaskRecovery(self.work)
        self.assertEqual(recovery.tick(),1)
        self.assertEqual(self.work.task_checkout.get(self.pid)['status'],'DELIVERED')
        self.assertEqual(TaskRecovery(self.work).tick(),0)
        self.assertEqual(len(self.paypal.captures),1)

    def test_cancelled_approval_can_never_capture_or_become_entitlement(self):
        self.approve();r=TaskRecovery(self.work)
        self.assertEqual(r.cancel(self.pid,self.approval)['status'],'CANCEL_REQUESTED')
        self.paypal.order['status']='APPROVED'
        self.work.task_checkout.capture(self.pid,self.approval)
        self.assertEqual(self.paypal.captures,[])
        self.paypal.capture(self.purchase['order_id'],'external-fixture')
        self.assertEqual(r.reconcile(self.pid)['status'],'REFUND_REQUIRED')
        self.assertFalse(self.work.task_checkout.get(self.pid)['entitled'])

    def test_lost_refund_response_is_read_back_and_never_reissued(self):
        self.pay();self.work.task_checkout.fulfill(self.pid)
        r=TaskRecovery(self.work);r.cancel(self.pid,self.approval)
        attempts=[]
        def refund(cid,amount,key):
            attempts.append(key);raise TimeoutError('fixture lost refund')
        self.paypal.refund=refund
        self.paypal.read_capture=lambda cid:{'id':cid,'status':'REFUNDED','amount':{'currency_code':'USD','value':self.purchase['plan']['amount']}}
        self.assertEqual(r.refund(self.pid,self.approval)['status'],'REFUNDED')
        self.assertEqual(r.refund(self.pid,self.approval)['status'],'REFUNDED')
        self.assertEqual(len(attempts),1)
        with self.assertRaises(MachineError):self.work.task_checkout.delivery(self.pid)
        self.assertEqual(self.work.task_checkout.reconcile(self.pid)['status'],'REFUNDED')

    def test_wrong_refund_readback_never_marks_completed(self):
        self.pay();r=TaskRecovery(self.work);r.cancel(self.pid,self.approval)
        self.paypal.refund=lambda *args:{'id':'TESTREFUND12345'}
        self.paypal.read_capture=lambda cid:{'id':'OTHERID12345','status':'REFUNDED','amount':{'currency_code':'USD','value':'1.00'}}
        with self.assertRaises(MachineError):r.refund(self.pid,self.approval)
        self.assertEqual(self.work.task_checkout.get(self.pid)['status'],'REFUND_UNKNOWN')

    def test_signed_webhook_is_advisory_idempotent_and_does_not_capture(self):
        self.approve();r=TaskRecovery(self.work)
        e={'id':'EVENT-00000001','resource':{'supplementary_data':{'related_ids':{'order_id':'TESTORDER12345'}}}}
        self.paypal.verify_webhook=lambda *args:None
        with patch.dict(os.environ,{'ENGINE_PAYPAL_WEBHOOK_ID':'WEBHOOK-12345'}):
            self.assertEqual(verified_webhook(self.paypal,{},e),'TESTORDER12345')
        r.webhook(self.pid,e)
        self.assertTrue(r.webhook(self.pid,e)['duplicate'])
        self.assertEqual(self.paypal.captures,[])
        self.assertFalse(self.work.task_checkout.get(self.pid)['entitled'])
        changed=copy.deepcopy(e);changed['extra']='tampered'
        with self.assertRaises(MachineError):r.webhook(self.pid,changed)

    def test_bad_signature_rejected_before_event_admission(self):
        def deny(*args):raise MachineError('PAYPAL_WEBHOOK_SIGNATURE_INVALID')
        self.paypal.verify_webhook=deny
        with patch.dict(os.environ,{'ENGINE_PAYPAL_WEBHOOK_ID':'WEBHOOK-12345'}),self.assertRaisesRegex(MachineError,'SIGNATURE_INVALID'):
            verified_webhook(self.paypal,{}, {'id':'EVENT-00000001'})

    def test_external_refund_revokes_existing_delivery_and_cannot_resurrect(self):
        self.pay();self.work.task_checkout.fulfill(self.pid)
        self.paypal.read_capture=lambda cid:{'id':cid,'status':'REFUNDED','amount':{'currency_code':'USD','value':self.purchase['plan']['amount']}}
        r=TaskRecovery(self.work)
        r.webhook(self.pid,{'id':'REFUND-EVENT001','event_type':'PAYMENT.CAPTURE.REFUNDED'})
        self.assertEqual(self.work.task_checkout.get(self.pid)['status'],'REFUNDED')
        with self.assertRaises(MachineError):self.work.task_checkout.delivery(self.pid)
        self.assertEqual(r.reconcile(self.pid)['status'],'REFUNDED')

    def test_cancel_racing_readback_never_grants_access(self):
        self.approve();self.paypal.capture('TESTORDER12345','external-fixture')
        r=TaskRecovery(self.work)
        original=self.paypal.read
        def racing(oid):
            result=original(oid);r.cancel(self.pid,self.approval);return result
        self.paypal.read=racing
        self.assertEqual(self.work.task_checkout.reconcile(self.pid)['status'],'CANCEL_REQUESTED')
        self.assertFalse(self.work.task_checkout.get(self.pid)['entitled'])
