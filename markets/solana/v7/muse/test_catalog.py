import json,unittest
from contract import ActivationState,NOTICE_VERSION,DATA_SCOPE,Rejected
from policy_catalog import validate_compact
class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.a=ActivationState(True,True,NOTICE_VERSION,DATA_SCOPE,True,True,60_000_000_000,False)
        self.r={'model':'meta/muse-spark-1.3-contributor','provider':'Meta','choices':[{'finish_reason':'stop','message':{'content':'{"c":2}'}}]}
    def validate(self,now=1):return validate_compact(self.r,started_ns=0,now_ns=now,activation=self.a)
    def test_no_price_or_execution_authority(self):
        p=self.validate();self.assertFalse(p['execution_authority']);self.assertTrue(p['requires_current_market_and_mandate']);self.assertNotIn('price',p['definition'])
    def test_unknown_boolean_duplicate_and_extra(self):
        for content in ['{"c":true}','{"c":4}','{"c":2,"price":1}','{"c":2,"c":3}']:
            self.r['choices'][0]['message']['content']=content
            with self.assertRaises(Rejected):self.validate()
    def test_cannot_reset_source_deadline(self):
        with self.assertRaises(Rejected):self.validate(30_000_000_000)
    def test_model_and_provider_fixed(self):
        self.r['model']='meta/muse-spark-1.3'
        with self.assertRaises(Rejected):self.validate()
    def test_incomplete_is_not_policy(self):
        self.r['choices'][0]['finish_reason']='length'
        with self.assertRaises(Rejected):self.validate()
if __name__=='__main__':unittest.main()
