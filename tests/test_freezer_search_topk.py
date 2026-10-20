import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.arbitrum_research import freezer_search as search
from economic_machine.arbitrum_research.graph import Graph
from test_arbitrum_graph import addr, block, h, tx


def exhaustive(base,chain,address,limit):
    """Original exhaustive algorithm, independent of the pruning predicate."""
    catalog=search.connect(Path(base)/'evidence/freezer-search-v1/catalogue.sqlite3')
    try:
        rows=catalog.execute('select s.* from shards s join wallet_shards w on w.shard=s.id where w.chain=? and w.wallet=? order by s.last desc limit 65',(chain,address)).fetchall()
        coverage=[dict(r) for r in catalog.execute('select first,last,id from shards where chain=? order by first',(chain,))]
    finally:catalog.close()
    edges=[];interactions=[];conflicts=[];sources=[]
    for row in rows[:64]:
        original=Path(row['path']);record=json.loads(row['body'])
        with search.resolve(base,row) as path:
            identities={p:search.sealed(p,digest) for p,digest in [(path,row['id']),(original.parent/'verification.json',record['report_sha256']),(original.parent/'source-discrepancies.json',record['discrepancy_sha256'])]}
            db=search.connect(path)
            try:
                for table,target in [('edges',edges),('interactions',interactions)]:
                    target.extend(dict(r,shard=row['id']) for r in db.execute(f'select * from {table} where sender=? or recipient=? order by block desc,id limit ?',(address,address,limit+1)))
            finally:db.close()
            discrepancies=json.loads((original.parent/'source-discrepancies.json').read_bytes())['records']
            conflicts.extend(dict(d,shard=row['id']) for d in discrepancies if address in (d['prior_sender'],d['snapshot_sender']))
            if any(search.file_identity(p)!=value for p,value in identities.items()):raise ValueError('SEARCH_SEALED_SHARD_CHANGED')
        sources.append(dict(shard=row['id'],first=row['first'],last=row['last']))
    def ordered(items):return sorted(items,key=lambda r:(-r['block'],r.get('id',''),r['shard']))[:limit]
    return dict(chain_id=chain,address=address,edges=ordered(edges),interactions=ordered(interactions),source_conflicts=ordered(conflicts),sources=sources,
        coverage=coverage,scope='registered_snapshot_shards',limited=len(rows)>64 or len(edges)>limit or len(interactions)>limit or len(conflicts)>limit,
        trace_available=False,whole_history_complete=False,current_balance_available=False,owner_inferred=False,independent_consensus_verified=False)


class TopKSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);(self.base/'evidence').mkdir();self.sequence=0

    def shard(self,first,last,*,counts=None,status=1,value=13,conflict=False):
        self.sequence+=1;folder=self.base/'evidence'/str(self.sequence);folder.mkdir()
        graph=Graph(folder/'index.sqlite3',initial_start=first,previous_block_hash=h(first) if first else None)
        rawsha=hashlib.sha256(str(self.sequence).encode()).hexdigest()
        records=[]
        for number in range(first,last+1):
            transactions=[]
            for index in range((counts or {}).get(number,1) if counts is None else counts.get(number,0)):
                transaction=tx(index,status=status,value=value);transaction['hash']=h(10000+number*100+index)
                transactions.append(transaction)
            records.append(block(number,transactions=transactions,traces=[]))
        graph.ingest(records,rawsha,first,last,dict(raw_sha256=rawsha,complete_cloud_readback=True,chain_id=42161))
        state=graph.checkpoint();graph.close()
        (folder/'verification.json').write_text(json.dumps(dict(state='VERIFIED_SEARCHABLE_SHARD',blocks=last-first+1,source_blob_readback=True,graph=state)))
        discrepancies=[dict(block=first,transaction=h(first),prior_sender=addr(1),snapshot_sender=addr(9),economic_edges_excluded=True)] if conflict else []
        (folder/'source-discrepancies.json').write_text(json.dumps(dict(records=discrepancies)))
        search.register(self.base,folder)
        return folder

    def compare(self,limit=3):
        expected=exhaustive(self.base,42161,addr(1),limit)
        with patch.object(search,'resolve',wraps=search.resolve) as resolver:
            actual=search.wallet(self.base,42161,addr(1),limit)
        source_metadata=actual['sources']
        actual_for_equality=dict(actual,sources=[{k:r[k] for k in ('shard','first','last')} for r in source_metadata])
        self.assertEqual(actual_for_equality,expected)
        self.assertEqual(resolver.call_count,sum(s['index_queried'] for s in source_metadata))
        return actual

    def test_older_graph_restore_skipped_but_conflicts_still_collected(self):
        self.shard(0,9,conflict=True);self.shard(20,23,counts={23:4})
        actual=self.compare()
        self.assertEqual([s['index_queried'] for s in actual['sources']],[True,False])
        self.assertEqual(actual['sources'][1]['index_skip_reason'],'strict_top_k_block_bound')
        self.assertEqual(len(actual['source_conflicts']),1)
        self.assertTrue(actual['limited']);self.assertFalse(actual['whole_history_complete'])

    def test_overlapping_ranges_can_skip_only_with_strict_top_block(self):
        self.shard(5,19);self.shard(10,20,counts={20:4})
        actual=self.compare()
        self.assertEqual([s['index_queried'] for s in actual['sources']],[True,False])

    def test_overlapping_range_with_higher_candidate_block_is_queried(self):
        self.shard(5,19);self.shard(10,20,counts={15:4})
        actual=self.compare()
        self.assertTrue(all(s['index_queried'] for s in actual['sources']))
        self.assertEqual(actual['edges'][0]['block'],19)

    def test_equal_block_ties_are_not_pruned(self):
        self.shard(5,10,counts={10:4});self.shard(10,12,counts={10:4})
        actual=self.compare()
        self.assertTrue(all(s['index_queried'] for s in actual['sources']))

    def test_exactly_limit_rows_does_not_change_limited_status(self):
        self.shard(0,1);self.shard(20,23,counts={23:3})
        actual=self.compare()
        self.assertTrue(all(s['index_queried'] for s in actual['sources']))
        self.assertTrue(actual['limited'])

    def test_insufficient_edges_requires_query_even_if_interactions_full(self):
        self.shard(0,1);self.shard(20,23,counts={23:4},status=0)
        actual=self.compare()
        self.assertTrue(all(s['index_queried'] for s in actual['sources']))
        self.assertTrue(all(r['block']<20 for r in actual['edges']))

    def test_insufficient_interactions_prevents_pruning(self):
        row=dict(first=0,last=9,chain=42161);record=dict(first=0,last=9,blocks=10)
        report=dict(state='VERIFIED_SEARCHABLE_SHARD',source_blob_readback=True,blocks=10,
            graph=dict(chain_id=42161,covered_first_block=0,covered_last_block=9,counts=dict(blocks=10)))
        self.assertIsNone(search._top_k_skip([dict(block=30)]*4,[dict(block=30)]*3,3,row,record,report))

    def test_changed_discrepancy_metadata_on_skipped_index_rejected(self):
        old=self.shard(0,9);self.shard(20,23,counts={23:4})
        self.compare()
        (old/'source-discrepancies.json').write_text('{"records":[]}\n')
        with self.assertRaisesRegex(ValueError,'SEALED'):
            search.wallet(self.base,42161,addr(1),3)

    def test_changed_verification_metadata_on_skipped_index_rejected(self):
        old=self.shard(0,9);self.shard(20,23,counts={23:4})
        with (old/'verification.json').open('ab') as f:f.write(b' ')
        with self.assertRaisesRegex(ValueError,'SEALED'):
            search.wallet(self.base,42161,addr(1),3)

    def test_catalogue_bound_without_matching_sealed_report_cannot_prune(self):
        old=self.shard(0,9);self.shard(20,23,counts={23:4})
        db=sqlite3.connect(self.base/'evidence/freezer-search-v1/catalogue.sqlite3')
        db.execute('UPDATE shards SET last=8 WHERE path=?',(str(old/'index.sqlite3'),));db.commit();db.close()
        actual=self.compare()
        self.assertTrue(all(s['index_queried'] for s in actual['sources']))

    def test_legacy_verification_without_bounds_falls_back_to_query(self):
        old=self.shard(0,9);self.shard(20,23,counts={23:4})
        path=old/'verification.json';body=json.loads(path.read_text());del body['graph']['covered_last_block'];path.write_text(json.dumps(body))
        db=sqlite3.connect(self.base/'evidence/freezer-search-v1/catalogue.sqlite3')
        record=json.loads(db.execute('SELECT body FROM shards WHERE path=?',(str(old/'index.sqlite3'),)).fetchone()[0])
        record['report_sha256']=search.file_sha(path)
        db.execute('UPDATE shards SET body=? WHERE path=?',(json.dumps(record),str(old/'index.sqlite3')));db.commit();db.close()
        actual=self.compare()
        self.assertTrue(all(s['index_queried'] for s in actual['sources']))

    def test_multiple_limits_match_exhaustive_results(self):
        for n in range(6):self.shard(n*10,n*10+3,counts={n*10+3:n+1},conflict=n%2==0)
        for limit in (1,2,3,5,10,100):
            with self.subTest(limit=limit):self.compare(limit)


if __name__=='__main__':unittest.main()
