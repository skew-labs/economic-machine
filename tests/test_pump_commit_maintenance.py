import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock,patch

from economic_machine.pump_research.address_index import AddressIndex
from economic_machine.pump_research.commit_maintenance import review_retained


class CommitMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);out=self.base/'evidence/wallet-map-v1';out.mkdir(parents=True)
        self.cache=out/'archive-ranges';self.cache.mkdir()
        index=AddressIndex(out/'address-index.sqlite3');index.close()
        db=sqlite3.connect(out/'graph.sqlite3');db.row_factory=sqlite3.Row;self.addCleanup(db.close)
        db.executescript("CREATE TABLE frontier(id TEXT PRIMARY KEY,cursor TEXT); INSERT INTO frontier VALUES('old','123');")
        self.g=NS(base=self.base,out=out,db=db)
        self.scope=NS(hash='a'*64,grant=dict(deadline_ms=9999999999999),guard=Mock(),reserve=Mock())
        self.audited=dict(scanned_blocks=2,next_after_id=2,more_blocks=False,policies_appended=1,blocks=[])
    def run_review(self,audit=None,priority_count=0,**kwargs):
        with (patch('economic_machine.pump_research.cli.proto',return_value=(None,object())) as proto,
              patch('economic_machine.pump_research.commit_maintenance.audit_cached_commits',return_value=audit or dict(self.audited)) as cached,
              patch('economic_machine.pump_research.commit_maintenance.assess_new_edges',return_value=dict(assessed=priority_count)) as priority,
              patch('economic_machine.pump_research.commit_maintenance.assess_edges',return_value=dict(assessed=3)) as edges,
              patch('economic_machine.pump_research.archive.get',side_effect=AssertionError('NETWORK_FORBIDDEN'))):
            result=review_retained(self.g,self.scope,**kwargs)
        return result,cached,edges,proto
    def test_new_edge_lane_keeps_historical_work_inside_total_budget(self):
        result,_,edges,_=self.run_review(priority_count=64)
        self.assertEqual(result['graph_priority']['assessed'],64)
        self.assertEqual(edges.call_args.kwargs['limit'],236)
    def test_fewer_new_edges_return_unused_budget_to_history(self):
        result,_,edges,_=self.run_review(priority_count=4)
        self.assertEqual(result['graph_priority']['assessed'],4)
        self.assertEqual(edges.call_args.kwargs['limit'],296)
    def test_single_edge_budget_never_advances_historical_cursor(self):
        result,_,edges,_=self.run_review(priority_count=1,max_edges=1)
        edges.assert_not_called()
        self.assertEqual(result['graph']['status'],'PRIORITY_USED_REVIEW_BUDGET')
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier').fetchone()[0],'123')
    def test_new_lane_runs_before_history_and_uses_capped_limit(self):
        order=[]
        with (patch('economic_machine.pump_research.cli.proto',return_value=(None,object())),
              patch('economic_machine.pump_research.commit_maintenance.audit_cached_commits',return_value=self.audited),
              patch('economic_machine.pump_research.commit_maintenance.assess_new_edges',side_effect=lambda *a,**k:order.append(('new',k['limit'])) or dict(assessed=3)),
              patch('economic_machine.pump_research.commit_maintenance.assess_edges',side_effect=lambda *a,**k:order.append(('history',k['limit'])) or dict(assessed=7))):
            review_retained(self.g,self.scope,max_edges=10)
        self.assertEqual(order,[('new',10),('history',7)])
    def test_offline_pass_appends_report_and_preserves_original_cursor_and_io(self):
        result,cached,edges,proto=self.run_review()
        self.assertEqual(result['state'],'OFFLINE_REVIEW_COMPLETE');self.assertEqual(result['network_requests'],0)
        self.scope.reserve.assert_not_called();self.assertEqual(cached.call_args.kwargs['limit'],2)
        self.assertEqual(cached.call_args.kwargs['object_roots'],(self.base/'data',self.g.out/'recovered'))
        self.assertEqual(edges.call_args.kwargs['limit'],300)
        row=self.g.db.execute('SELECT * FROM commit_review_runs').fetchone()
        self.assertEqual(hashlib.sha256(Path(result['report_path']).read_bytes()).hexdigest(),row['report_sha256'])
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier').fetchone()[0],'123')
    def test_incomplete_cache_pass_resumes_separate_cursor(self):
        first,_,_,_=self.run_review(audit=dict(self.audited,more_blocks=True))
        second,cached,_,_=self.run_review(audit=dict(self.audited,next_after_id=4))
        self.assertEqual(cached.call_args.kwargs['after_id'],2)
        self.assertEqual(second['cache_pass'],first['cache_pass']);self.assertEqual(second['cache_cursor'],4)
        self.assertNotEqual(first['report_path'],second['report_path'])
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM commit_review_runs').fetchone()[0],2)
    def test_current_cache_pass_does_not_reparse_but_still_checks_graph(self):
        self.run_review();result,cached,edges,proto=self.run_review()
        cached.assert_not_called();proto.assert_not_called();edges.assert_called_once()
        self.assertEqual(result['cache']['state'],'CACHE_PASS_CURRENT')
    def test_new_cache_file_starts_new_audit_pass_after_old_pass_completed(self):
        first,_,_,_=self.run_review();st=self.cache.stat()
        os.utime(self.cache,ns=(st.st_atime_ns,st.st_mtime_ns+1000000))
        second,cached,_,_=self.run_review()
        self.assertEqual(cached.call_args.kwargs['after_id'],0);self.assertEqual(second['cache_pass'],first['cache_pass']+1)
        self.assertTrue(Path(first['report_path']).exists())
    def test_cache_change_midpass_finishes_old_cursor_then_restarts_separate_pass(self):
        first,_,_,_=self.run_review(audit=dict(self.audited,more_blocks=True));st=self.cache.stat()
        os.utime(self.cache,ns=(st.st_atime_ns,st.st_mtime_ns+1000000))
        second,cached,_,_=self.run_review(audit=dict(self.audited,next_after_id=4))
        self.assertEqual(cached.call_args.kwargs['after_id'],2)
        self.assertEqual(second['cache_revision'],first['cache_revision'])
        third,cached,_,_=self.run_review()
        self.assertEqual(cached.call_args.kwargs['after_id'],0);self.assertEqual(third['cache_pass'],first['cache_pass']+1)
    def test_deadline_guard_runs_before_any_review_write(self):
        self.scope.guard.side_effect=ValueError('CORE_DEADLINE')
        with self.assertRaisesRegex(ValueError,'CORE_DEADLINE'):self.run_review()
        self.assertEqual(list(self.g.out.glob('COMMIT-REVIEW-*.json')),[])
    def test_offline_failure_is_reported_and_original_cursor_retained(self):
        with (patch('economic_machine.pump_research.cli.proto',return_value=(None,object())),
              patch('economic_machine.pump_research.commit_maintenance.audit_cached_commits',side_effect=ValueError('CACHE_HASH')),
              patch('economic_machine.pump_research.commit_maintenance.assess_edges') as edges):
            with self.assertRaisesRegex(ValueError,'CACHE_HASH'):review_retained(self.g,self.scope)
        edges.assert_not_called()
        row=json.loads(self.g.db.execute('SELECT body FROM commit_review_runs').fetchone()[0])
        self.assertEqual(row['state'],'OFFLINE_REVIEW_FAILED');self.assertEqual(row['cache_cursor'],0)
        self.assertEqual(self.g.db.execute('SELECT cursor FROM frontier').fetchone()[0],'123')
    def test_review_lock_busy_does_not_wait_or_start_work(self):
        with (self.g.out/'commit-review.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result,cached,edges,_=self.run_review()
        self.assertEqual(result['state'],'COMMIT_REVIEW_BUSY');cached.assert_not_called();edges.assert_not_called()
    def test_review_limits_checked_before_scope_or_source_access(self):
        for values in (dict(max_blocks=0),dict(max_blocks=9),dict(max_blocks=True),dict(max_edges=1001)):
            with self.assertRaises(ValueError):review_retained(self.g,self.scope,**values)
        self.scope.guard.assert_not_called()


if __name__=='__main__':unittest.main()
