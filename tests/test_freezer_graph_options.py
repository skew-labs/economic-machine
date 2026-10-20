import unittest
from economic_machine.arbitrum_research.freezer_graph_options import options

class GraphOptionsTests(unittest.TestCase):
 def test_off_preserves_old_constructor(self):self.assertEqual(options(42161,True,'0','0'),({},{}))
 def test_new_modes_are_explicit(self):
  self.assertEqual(options(42161,True,'1','512'),({'graph_cache_mib':512,'graph_materialization':'memory_sealed_v1'},
   {'cache_mib':512,'memory_build':True}))
 def test_old_policy_cannot_change_on_resume(self):self.assertEqual(options(42161,True,'1','512',{}),({},{}))
 def test_new_policy_can_resume_with_flags_off(self):
  policy={'graph_cache_mib':512,'graph_materialization':'memory_sealed_v1'}
  self.assertEqual(options(42161,True,'0','0',policy),(policy,{'cache_mib':512,'memory_build':True}))
 def test_other_chain_and_unprepared_rejected(self):
  for chain,prepared in [(4663,True),(42161,False)]:
   with self.assertRaisesRegex(ValueError,'GRAPH_EXECUTION_SCOPE'):options(chain,prepared,'1','512')
 def test_settings_bounded(self):
  for memory,cache in [('yes','512'),('1','1024'),('1','-1')]:
   with self.assertRaisesRegex(ValueError,'GRAPH_EXECUTION_SETTING'):options(42161,True,memory,cache)
 def test_retained_policy_bad_types_rejected(self):
  for policy in [{'graph_cache_mib':True},{'graph_cache_mib':'512'},{'graph_materialization':'unknown'},{'graph_cache_mib':256}]:
   with self.assertRaisesRegex(ValueError,'GRAPH_EXECUTION_POLICY'):options(42161,True,'0','0',policy)

if __name__=='__main__':unittest.main()
