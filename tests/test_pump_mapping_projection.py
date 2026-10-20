from types import SimpleNamespace
import unittest
from economic_machine.pump_research.mapping_projection import native_balance_observation,project_block


class Projection(unittest.TestCase):
    def test_failed_fees_retained(self):
        tx=dict(signature='s',account_keys=['a','b'],success=False,custom_evidence={'a':1})
        meta=SimpleNamespace(pre_balances=[100,20],post_balances=[95,20],fee=5)
        block=dict(transaction_nodes=1,transactions=[tx],decode_errors=[])
        p=project_block(block,{'s':meta})
        self.assertFalse(p['transactions'][0]['success'])
        self.assertEqual(p['transactions'][0]['fee_lamports'],5)
        self.assertEqual(p['transactions'][0]['custom_evidence'],{'a':1})
        self.assertEqual(p['filtered_transactions'],0)

    def test_balance_delta_not_edge(self):
        result=native_balance_observation({'account_keys':['a','b']},SimpleNamespace(pre_balances=[100,20],post_balances=[85,30],fee=5))
        self.assertEqual(result['total_delta_lamports'],-5)
        self.assertFalse(result['native_deltas_are_pairwise_transfers'])

    def test_incomplete_balances_rejected(self):
        with self.assertRaisesRegex(ValueError,'SHAPE'):
            native_balance_observation({'account_keys':['a','b']},SimpleNamespace(pre_balances=[100],post_balances=[95],fee=5))

    def test_membership_rejected(self):
        with self.assertRaisesRegex(ValueError,'MEMBERSHIP'):
            project_block({'transaction_nodes':1,'transactions':[{'signature':'s'}],'decode_errors':[]},{})

    def test_partial_rejected(self):
        with self.assertRaisesRegex(ValueError,'PARTIAL'):
            project_block({'transaction_nodes':0,'transactions':[],'decode_errors':['missingmeta']},{})


if __name__=='__main__':unittest.main()
