import json
from pathlib import Path
import tempfile
import unittest
from economic_machine.arbitrum_research.graph import Graph
from economic_machine.arbitrum_research.freezer_search import register,wallet
from test_arbitrum_graph import block,addr,h

class SearchTest(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);(self.base/'evidence').mkdir()
 def tearDown(self):self.tmp.cleanup()
 def shard(self,start=0,chain=42161,conflict=False):
  folder=self.base/'evidence'/f'{chain}-{start}';folder.mkdir();b=block(start,traces=[]);b['transactions'][0]['hash']=h(100+start)
  if conflict:b['transactions'][0]['sender_conflict']=True
  graph=Graph(folder/'index.sqlite3',initial_start=start,previous_block_hash=h(start) if start else None,chain_id=chain)
  proof=dict(raw_sha256='a'*64,complete_cloud_readback=True,chain_id=chain)
  graph.ingest([b],'a'*64,start,start,proof);state=graph.checkpoint();graph.close()
  (folder/'verification.json').write_text(json.dumps(dict(state='VERIFIED_SEARCHABLE_SHARD',blocks=1,source_blob_readback=True,graph=state)))
  discrepancies=[dict(block=start,transaction=h(100+start),prior_sender=addr(3),snapshot_sender=addr(1),economic_edges_excluded=True)] if conflict else []
  (folder/'source-discrepancies.json').write_text(json.dumps(dict(records=discrepancies)))
  register(self.base,folder);return folder
 def test_multiple_shards_and_chain_separation(self):
  self.shard();self.shard(1);self.shard(chain=4663)
  result=wallet(self.base,42161,addr(1));self.assertEqual(len(result['interactions']),2);self.assertEqual(len(result['coverage']),2)
  self.assertFalse(result['whole_history_complete']);self.assertFalse(result['current_balance_available'])
  self.assertEqual(len(wallet(self.base,4663,addr(1))['interactions']),1)
 def test_conflicting_prior_sender_remains_searchable_without_edge(self):
  self.shard(conflict=True);r=wallet(self.base,42161,addr(3));self.assertEqual(len(r['source_conflicts']),1);self.assertEqual(r['edges'],[])
  self.assertEqual(wallet(self.base,42161,addr(1))['edges'],[])
 def test_changed_sealed_graph_is_rejected(self):
  folder=self.shard()
  wallet(self.base,42161,addr(1))
  with (folder/'index.sqlite3').open('ab') as f:f.write(b'change')
  with self.assertRaisesRegex(ValueError,'SEALED'):wallet(self.base,42161,addr(1))
 def test_changed_conflict_evidence_is_rejected(self):
  folder=self.shard();(folder/'source-discrepancies.json').write_text('{"records":[]}\n')
  with self.assertRaisesRegex(ValueError,'SEALED'):wallet(self.base,42161,addr(1))
 def test_query_limits_and_unknown_address(self):
  self.shard();self.assertEqual(wallet(self.base,42161,addr(500))['sources'],[])
  for address,n in [('bad',10),(addr(1),0),(addr(1),1001)]:
   with self.assertRaises(ValueError):wallet(self.base,42161,address,n)
 def test_duplicate_registration_is_idempotent(self):
  folder=self.shard();register(self.base,folder);self.assertEqual(len(wallet(self.base,42161,addr(1))['interactions']),1)
 def test_disconnected_parent_boundary_rejected(self):
  self.shard();folder=self.base/'evidence'/'bad-parent';folder.mkdir();b=block(1,traces=[]);b['header']['parentHash']=h(999)
  graph=Graph(folder/'index.sqlite3',initial_start=1,previous_block_hash=h(999));proof=dict(raw_sha256='b'*64,complete_cloud_readback=True,chain_id=42161)
  graph.ingest([b],'b'*64,1,1,proof);state=graph.checkpoint();graph.close()
  (folder/'verification.json').write_text(json.dumps(dict(state='VERIFIED_SEARCHABLE_SHARD',blocks=1,source_blob_readback=True,graph=state)))
  (folder/'source-discrepancies.json').write_text('{"records":[]}')
  with self.assertRaisesRegex(ValueError,'PARENT_BOUNDARY'):register(self.base,folder)

if __name__=='__main__':unittest.main()
