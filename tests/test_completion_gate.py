import copy,unittest
from economic_machine.arbitrum_research.completion_gate import evaluate

class GateTest(unittest.TestCase):
 def case(self):
  return dict(now_ms=1800000,deadline_ms=7200000,chains={name:dict(transaction_search_connected=True,remaining_work_known=True,workload_scope_matches_remaining=True,unresolved_required_gaps=0,remaining_units=1000,
    sustained_windows=[dict(start_ms=i*600000,seconds=600,units=6000,cold_source=True,search_committed=True) for i in range(3)]) for name in ('solana','arbitrum','robinhood')},
    budget=dict(projected_completion_krw=100,projected_completion_bytes=100,limit_krw=1000,limit_bytes=1000))
 def test_only_complete_evidence_stops_optimization(self):
  r=evaluate(**self.case());self.assertEqual(r['state'],'STOP_OPTIMIZATION_KEEP_COLLECTION');self.assertFalse(r['full_mapping_complete'])
 def test_decoder_only_does_not_qualify(self):
  c=self.case();c['chains']['solana']['sustained_windows'][0]['search_committed']=False
  self.assertEqual(evaluate(**c)['state'],'CONTINUE_OPTIMIZATION')
 def test_overlap_does_not_count_twice(self):
  c=self.case();c['chains']['arbitrum']['sustained_windows'][1]['start_ms']=0
  self.assertIn('arbitrum:OVERLAPPING_RATE_WINDOWS',evaluate(**c)['reasons'])
 def test_unmeasured_history_and_trace_gaps_remain_blockers(self):
  c=self.case();c['chains']['solana'].update(remaining_work_known=False,unresolved_required_gaps=1)
  r=evaluate(**c);self.assertIn('solana:REMAINING_WORK_UNKNOWN',r['reasons']);self.assertIn('solana:REQUIRED_DATA_GAPS',r['reasons'])
 def test_budget_or_deadline_does_not_reset(self):
  c=self.case();c['budget']['projected_completion_krw']=1001
  self.assertIn('FULL_COMPLETION_RESOURCE_LIMIT',evaluate(**c)['reasons'])
  c['now_ms']=c['deadline_ms'];self.assertIn('DEADLINE_REACHED',evaluate(**c)['reasons'])
 def test_short_or_warm_runs_not_sustained(self):
  for field,value in [('seconds',20),('cold_source',False)]:
   c=self.case();c['chains']['robinhood']['sustained_windows'][0][field]=value
   self.assertIn('robinhood:SUSTAINED_RATE_NOT_PROVEN',evaluate(**c)['reasons'])

 def test_early_classic_interval_does_not_prove_modern_full_chain(self):
  c=self.case();c['chains']['arbitrum']['workload_scope_matches_remaining']=False
  self.assertIn('arbitrum:REPRESENTATIVE_WORKLOAD_NOT_PROVEN',evaluate(**c)['reasons'])
 def test_future_or_invalid_samples_never_admit(self):
  for field,value in [('start_ms',1800000),('seconds',float('nan')),('units',True),('cold_source',1)]:
   c=self.case();c['chains']['solana']['sustained_windows'][0][field]=value
   self.assertIn('solana:SUSTAINED_RATE_NOT_PROVEN',evaluate(**c)['reasons'])
 def test_nan_resource_projection_is_not_within_budget(self):
  c=self.case();c['budget']['projected_completion_krw']=float('nan')
  self.assertIn('FULL_COMPLETION_RESOURCE_COST_UNKNOWN',evaluate(**c)['reasons'])

if __name__=='__main__':unittest.main()
