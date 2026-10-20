import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from economic_machine.pump_research.address_index import AddressIndex,search
from economic_machine.pump_research.precision import enrich_transaction,owner_evidence
from economic_machine.pump_research.research_quality import audit_root
from economic_machine.pump_research.store import encode,canonical
from economic_machine.pump_research.wallet_map import Graph,digest

def key(n):return encode(bytes([n])*32)
def sig(n):return encode(bytes([n])*64)
def balance(index,owner,amount='100',mint=None):
    return NS(account_index=index,owner=owner,mint=mint or key(3),ui_token_amount=NS(amount=amount,decimals=6))

class PrecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        (self.base/'evidence').mkdir();self.g=Graph(self.base);self.addCleanup(self.g.close)
        self.index=AddressIndex(self.g.out/'address-index.sqlite3');self.addCleanup(self.index.close)
        self.scope=NS(guard=lambda:None)
    def tx(self,n,order,ix,slot=250000000,stamp=1706000000,owners=False,success=True):
        tx=dict(signature=sig(n),slot=slot,index=order,success=success,account_keys=[key(1),key(2),key(3)],
            transfers=[dict(ix=ix,source=key(1),destination=key(2),mint=key(3),amount='100',kind='spl_token_account_transfer',authority=key(9))],
            activity=[dict(ix=ix,wallet=key(1),mint=key(3),action='buy')],launches=[])
        tx['instruction_commit_evidence']=dict(version=1,committed_inner_orders=[ix] if ix%65536 else [],errors=[])
        if owners:enrich_transaction(tx,NS(pre_token_balances=[balance(1,key(4),'0')],post_token_balances=[balance(1,key(4))]))
        return dict(slot=slot,time=stamp,transactions=[tx],transaction_nodes=1,decode_errors=[])
    def job(self,slot=250000000,stamp=1706000000000,order=(2,2)):
        with self.g.db:j=self.g.enqueue(key(10),key(1),slot,stamp,0,'indexed_forward',after_order=order)
        return dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?',(j,)).fetchone())
    def ingest(self,n,order,ix,**kwargs):
        b=self.tx(n,order,ix,**kwargs);self.index.ingest(b,dict(car_sha256=str(n)*64));return b
    def test_same_slot_before_equal_after_and_next_slot(self):
        self.ingest(1,1,9);self.ingest(2,2,2);self.ingest(3,2,3);self.ingest(4,3,0)
        self.ingest(5,0,0,slot=250000001)
        r=self.g.indexed_forward(self.job(),self.scope)
        self.assertEqual(r['indexed_transfers'],3);self.assertEqual(r['indexed_activity'],3)
        self.assertTrue(r['same_slot_after_anchor_supported'])
    def test_unknown_order_excludes_same_slot_not_invented_start(self):
        self.ingest(1,99,99);self.ingest(2,0,0,slot=250000001)
        r=self.g.indexed_forward(self.job(order=None),self.scope);self.assertEqual(r['indexed_transfers'],1)
        self.assertEqual(r['ordering'],'later_slot_only')
    def test_unknown_slot_requires_real_time_and_excludes_undated(self):
        self.ingest(1,1,0,stamp=None);self.ingest(2,2,0,stamp=1706000000);self.ingest(3,3,0,stamp=1706000001)
        r=self.g.indexed_forward(self.job(slot=None,order=None),self.scope)
        self.assertEqual(r['indexed_transfers'],1);self.assertEqual(r['ordering'],'block_time_only')
    def test_late_backfill_and_restart_do_not_move_anchor(self):
        j=self.job();self.ingest(1,5,0);self.g.indexed_forward(j,self.scope)
        self.ingest(2,3,0);self.ingest(3,1,0)
        j=dict(self.g.db.execute('SELECT * FROM frontier WHERE id=?',(j['id'],)).fetchone())
        r=self.g.indexed_forward(j,self.scope);self.assertEqual(r['indexed_transfers'],1)
        self.assertEqual(self.g.anchor(j)['tx_order'],2)
    def test_failed_and_dead_stream_never_add_edges(self):
        self.ingest(1,3,0,success=False)
        b=self.tx(2,4,0);self.index.ingest(b,dict(car_sha256='b'*64,source='ticking_retained'))
        self.index.dead_slot(b['slot'])
        self.assertEqual(self.g.indexed_forward(self.job(),self.scope)['indexed_transfers'],0)
    def test_metadata_owner_is_not_transfer_delegate(self):
        self.ingest(1,3,0,owners=True);self.g.indexed_forward(self.job(),self.scope)
        e=json.loads(self.g.db.execute("SELECT body FROM edges WHERE kind='indexed_transfer'").fetchone()[0])
        d=e['destination_owner_evidence'];self.assertEqual(d['boundary_owner'],key(4));self.assertNotEqual(d['boundary_owner'],key(9))
        self.assertFalse(d['instruction_time_owner_proven']);self.assertFalse(e['path_funds_continuity_proven'])
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM frontier WHERE wallet=?',(key(4),)).fetchone()[0],0)
    def test_changed_one_sided_missing_owner_not_collapsed(self):
        tx=self.tx(1,3,0)['transactions'][0]
        for pre,post,status in [([balance(1,key(4))],[balance(1,key(5))],'changed_owner'),
             ([],[balance(1,key(4))],'one_boundary_only'),([balance(1,'')],[balance(1,'')],'owner_not_recorded'),([],[],'missing_metadata')]:
            enrich_transaction(tx,NS(pre_token_balances=pre,post_token_balances=post))
            e=owner_evidence(tx['token_owner_snapshots'],key(2),key(3));self.assertEqual(e['status'],status);self.assertIsNone(e['boundary_owner'])
    def test_invalid_account_index_and_decimal_amount_rejected(self):
        tx=self.tx(1,3,0)['transactions'][0]
        enrich_transaction(tx,NS(pre_token_balances=[balance(99,key(4)),balance(1,key(4),'1.1')],post_token_balances=[]))
        self.assertEqual(tx['token_owner_snapshots'],[]);self.assertEqual(len(tx['precision_errors']),2)
    def test_replay_enriches_existing_block_without_duplicate_transactions(self):
        b=self.ingest(1,3,0);enrich_transaction(b['transactions'][0],NS(pre_token_balances=[balance(1,key(4))],post_token_balances=[balance(1,key(4))]))
        self.assertFalse(self.index.ingest(b,dict(car_sha256='1'*64)))
        self.assertFalse(self.index.ingest(b,dict(car_sha256='1'*64)))
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM transactions').fetchone()[0],1)
        self.assertEqual(self.index.db.execute('SELECT count(*) FROM token_owners').fetchone()[0],2)
        self.assertEqual(search(self.index.path,key(1))['transactions'][0]['transfers'][0]['destination_owner_evidence']['boundary_owner'],key(4))
    def test_migration_never_treats_advanced_cursor_as_original_anchor(self):
        with self.g.db:
            local=self.g.enqueue(key(10),key(1),250000000,1706000000000,0,after_order=(2,2))
            self.g.db.execute('DELETE FROM frontier_anchors')
            self.g.db.execute('UPDATE frontier SET cursor=? WHERE id=?',(canonical([250000000,9,9]),local))
            j=self.g.enqueue(key(10),key(1),250000000,1706000000000,0,'indexed_forward')
            self.g.db.execute("UPDATE frontier SET cursor='42',state='indexed_source_exhausted' WHERE id=?",(j,))
        self.g.migrate_precision();self.g.migrate_precision()
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM frontier_anchors').fetchone()[0],0)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM precision_cursor_history').fetchone()[0],'42')
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier WHERE id=?',(j,)).fetchone()[0],'null')
    def test_original_cursor_hash_recovers_anchor(self):
        with self.g.db:
            self.g.enqueue(key(10),key(1),250000000,1706000000000,0,after_order=(2,2));self.g.db.execute('DELETE FROM frontier_anchors')
            j=self.g.enqueue(key(10),key(1),250000000,1706000000000,0,'indexed_forward')
        self.g.migrate_precision();a=self.g.db.execute('SELECT tx_order,ix FROM frontier_anchors WHERE job=?',(j,)).fetchone()
        self.assertEqual(tuple(a),(2,2))
    def test_advanced_anchor_restored_only_by_matching_original_job_hash(self):
        self.ingest(1,2,2)
        with self.g.db:
            local=self.g.enqueue(key(10),key(1),250000000,1706000000000,0,after_order=(2,2));self.g.db.execute('DELETE FROM frontier_anchors')
            self.g.db.execute('UPDATE frontier SET cursor=? WHERE id=?',(canonical([250000000,9,9]),local))
            j=self.g.enqueue(key(10),key(1),250000000,1706000000000,0,'indexed_forward')
        self.g.recover_original_anchors(self.scope)
        self.assertEqual(tuple(self.g.db.execute('SELECT tx_order,ix FROM frontier_anchors WHERE job=?',(j,)).fetchone()),(2,2))
        self.assertEqual(self.g.state('precision_anchor_recovery_v2')['recovered_from_original_identity'],1)
    def test_rate_limit_retry_preserves_cursor_and_missing_data_stays_blocked(self):
        j=self.job();cursor='{"transfer":123}'
        with self.g.db:
            self.g.db.execute("UPDATE frontier SET state='source_blocked',cursor=? WHERE id=?",(cursor,j['id']))
            self.g.set('source_error:'+j['id'],dict(error_type='HTTPError',error='HTTP Error 429: Too Many Requests'))
        self.g.retry_source_errors(1000);self.g.retry_source_errors(60999)
        self.assertEqual(self.g.db.execute('SELECT state FROM frontier').fetchone()[0],'source_blocked')
        self.g.retry_source_errors(61000)
        self.assertEqual(tuple(self.g.db.execute('SELECT state,cursor FROM frontier').fetchone()),('pending',cursor))
        with self.g.db:
            self.g.db.execute("UPDATE frontier SET state='source_blocked'");self.g.set('source_error:'+j['id'],dict(error_type='ValueError',error='MAP_TIME_SEARCH_MISSING_RANGE'))
        self.g.retry_source_errors(1000000);self.assertEqual(self.g.db.execute('SELECT state FROM frontier').fetchone()[0],'source_blocked')
    def test_quality_separates_observation_duplicates_and_source_tiers(self):
        with self.g.db:
            self.g.edge(key(10),key(1),key(2),'sol_transfer',1,None,signature=sig(1),ix=0,finality='archive_content_verified')
            self.g.edge(key(10),key(1),key(2),'indexed_transfer',1,1706000000000,signature=sig(1),ix=0,source='reconstructed_public_block',precision_version=1)
            self.g.edge(key(10),key(2),key(3),'funding_record',2,1706000000001,source='core_arkm_funding',source_rowid=1)
        r=audit_root(dict(mint=key(10)),self.g.db.execute('SELECT * FROM edges'))
        self.assertEqual(r['distinct_observation_records'],2);self.assertEqual(r['repeated_event_observations'],1)
        self.assertEqual(r['evidence_tiers'],dict(archive_content=1,external_dataset=1));self.assertFalse(r['same_funds_continuity_proven'])
    def test_equal_amounts_do_not_deduplicate_without_original_signature_identity(self):
        with self.g.db:
            for row in (1,2,3):self.g.edge(key(10),key(1),key(3),'later_dataset_buy',1,1706000000000,
                source='core_pump_event_default',source_rowid=row,amount='100')
        rows=self.g.db.execute('SELECT * FROM edges').fetchall()
        self.assertEqual(audit_root(dict(mint=key(10)),rows)['distinct_observation_records'],3)
        identities={str(i):dict(event_identity='source-confirmed-pair',original_signature=sig(1),parse_version=i) for i in (1,2)}
        r=audit_root(dict(mint=key(10)),rows,identities)
        self.assertEqual(r['distinct_observation_records'],2);self.assertEqual(r['repeated_event_observations'],1)
    def test_payer_is_not_reported_as_creator(self):
        b=self.tx(1,3,0);b['transactions'][0]['launches']=[dict(ix=4,payer=key(1),creator='',mint=key(3))]
        self.index.ingest(b,dict(car_sha256='a'*64))
        action=self.index.db.execute('SELECT action FROM activity WHERE ix=4').fetchone()[0]
        self.assertEqual(action,'launch_payer')

if __name__=='__main__':unittest.main()
