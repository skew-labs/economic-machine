import tempfile
import unittest
from economic_machine.pump_research.signature_census import Census


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.policy={'epochs':[0,1,1009],'deadline_ms':9999999999999}
        self.c=Census(self.tmp.name,self.policy)
    def tearDown(self):self.c.close();self.tmp.cleanup()
    def result(self,epoch,n=5):return dict(epoch=epoch,provider_signature_entries=n,complete_canonical_history_proven=False)
    def test_priority(self):self.assertEqual(self.c.next_epoch(),1009)
    def test_completion_does_not_mean_canonical(self):
        self.c.begin(1009);self.c.finish(1009,self.result(1009))
        s=self.c.snapshot();self.assertEqual(s['counted_epochs'],1);self.assertEqual(s['unknown_epochs'],2)
        self.assertFalse(s['canonical_transaction_denominator_verified'])
    def test_cooldown_preserved(self):
        self.c.begin(1009);self.c.defer(1009,'SOURCE_BACKOFF',9999999999999,{'reason':'429'})
        self.c.close();self.c=Census(self.tmp.name,self.policy)
        self.assertNotEqual(self.c.next_epoch(),1009)
    def test_crash_running_job_requeued(self):
        self.c.begin(1009);self.c.close();self.c=Census(self.tmp.name,self.policy)
        self.assertEqual(self.c.db.execute('SELECT state FROM jobs WHERE epoch=1009').fetchone()[0],'QUEUED')
    def test_changed_policy_rejected(self):
        with self.assertRaisesRegex(ValueError,'POLICY_CHANGED'):Census(self.tmp.name,dict(self.policy,epochs=[2]))
    def test_result_conflict_rejected(self):
        self.c.begin(1009);self.c.finish(1009,self.result(1009))
        with self.assertRaisesRegex(ValueError,'COUNT_CHANGED'):self.c.finish(1009,self.result(1009,6))
    def test_failed_provider_is_not_zero(self):
        self.c.begin(1009);self.c.defer(1009,'PROVIDER_UNAVAILABLE',9999999999999,{'http_status':404})
        s=self.c.snapshot();self.assertEqual(s['unknown_epochs'],3);self.assertEqual(s['counted_epochs'],0)


if __name__=='__main__':unittest.main()
