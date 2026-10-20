import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from economic_machine.pump_research.coverage import Coverage, DEADLINE_MS
from economic_machine.pump_research.coverage_projection import sync_retained, report_retained, close_retained
from economic_machine.pump_research.store import canonical, encode


def key(number):
    return encode(number.to_bytes(32, 'big'))


class CoverageProjectionTests(unittest.TestCase):
    def setUp(self):
        clock = patch('economic_machine.pump_research.coverage.time.time_ns', return_value=(DEADLINE_MS-1000)*1000000)
        clock.start(); self.addCleanup(clock.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name)
        db = sqlite3.connect(out/'graph.sqlite3')
        db.row_factory = sqlite3.Row
        db.executescript('''CREATE TABLE edges(id TEXT PRIMARY KEY,root TEXT,source TEXT,target TEXT,
            kind TEXT,slot INTEGER,stamp INTEGER,body TEXT);
            CREATE TABLE frontier(id TEXT PRIMARY KEY,body TEXT);''')
        self.addCleanup(db.close)
        self.graph = SimpleNamespace(db=db,out=out)
        self.addCleanup(close_retained,self.graph)
        self.guards = []
        self.scope = SimpleNamespace(guard=lambda:self.guards.append(1))
        self.sequence = 0
        self.sender = key(1)
        self.recipient = key(2)
        self.signature = encode(bytes([7])*64)
        self.verdicts={}
        assessments=patch('economic_machine.pump_research.coverage_projection._assessments',
            side_effect=lambda graph,ids:{ident:self.verdicts.get(ident,dict(instruction_commit_status='committed',
                assessment_current=True,revision='synthetic-test-1')) for ident in ids})
        assessments.start();self.addCleanup(assessments.stop)

    def edge(self, sender=None, target=None, root=None, kind='indexed_transfer', slot=400000000,
             body=None, **overrides):
        self.sequence += 1
        ident = 'edge-'+str(self.sequence)
        evidence = dict(signature=self.signature, source='sparse_archive', source_sha256='a'*64,
            symbol='SOL', amount='9', ix=self.sequence, transfer_kind='system_transfer')
        evidence.update(overrides)
        with self.graph.db:
            self.graph.db.execute('INSERT INTO edges VALUES(?,?,?,?,?,?,?,?)',
                (ident,root or key(99),sender or self.sender,target or self.recipient,kind,slot,None,
                 canonical(evidence) if body is None else body))
        return ident

    def reader(self):
        reader = Coverage(self.graph.out/'coverage.sqlite3',read_only=True)
        self.addCleanup(reader.close)
        return reader

    def test_transfer_projects_only_actual_endpoints_and_not_research_root(self):
        edge = self.edge()
        result = sync_retained(self.graph,self.scope)
        self.assertEqual(result['projected'],1)
        self.assertEqual(result['network_calls'],0)
        self.assertEqual(result['model_calls'],0)
        reader = self.reader()
        sender = reader.report(self.sender)
        recipient = reader.report(self.recipient)
        self.assertEqual(sender['latest_observed_slot'],400000000)
        self.assertEqual(recipient['latest_observed_slot'],400000000)
        self.assertEqual(sender['trace_evidence']['outgoing'][0]['counterparty'],self.recipient)
        self.assertEqual(recipient['trace_evidence']['incoming'][0]['counterparty'],self.sender)
        self.assertIn('graph_edge:'+edge,sender['latest_observation_refs'])
        self.assertEqual(reader.report(key(99))['sources'],[])
        self.assertFalse(sender['same_funds_continuity_proven'])

    def test_single_observation_never_becomes_whole_slot_or_available_history(self):
        self.edge()
        sync_retained(self.graph,self.scope)
        report = self.reader().report(self.sender)
        self.assertEqual(report['spans']['available'],[])
        inspected = report['spans']['inspected'][0]
        self.assertEqual((inspected['start_slot'],inspected['end_slot']),(400000000,400000000))
        self.assertEqual(inspected['method'],'retained_single_transaction_reference')
        self.assertIn('rest of this slot and block is unknown',inspected['reason'])
        self.assertFalse(report['sources'][0]['entire_source_inspected'])
        self.assertEqual(report['sources'][0]['state'],'scanning')
        self.assertFalse(report['entire_wallet_lifetime_verified'])

    def test_raw_and_ticking_processed_sources_are_separate_even_same_slot(self):
        self.edge()
        self.edge(source='ticking_retained',source_sha256='b'*64,finality='filtered_processed_stream',
                  transaction_signature_verified=False)
        result = sync_retained(self.graph,self.scope)
        report = self.reader().report(self.sender)
        self.assertEqual(result['tiers'],{'retained_raw_transaction_record':1,'ticking_processed_observation':1})
        self.assertEqual({s['scope']['kind'] for s in report['sources']},
                         {'retained_raw_transaction_record','ticking_processed_observation'})
        self.assertEqual(len(report['sources']),2)
        self.assertTrue(all(not s['scope']['independent_consensus_verified'] for s in report['sources']))

    def test_activity_marks_actor_not_token_as_recipient_wallet(self):
        self.edge(kind='early_buy',target=key(10))
        sync_retained(self.graph,self.scope)
        reader = self.reader()
        actor = reader.report(self.sender)
        self.assertEqual(actor['latest_observed_slot'],400000000)
        self.assertEqual(actor['trace_evidence']['outgoing'],[])
        self.assertEqual(reader.report(key(10))['sources'],[])

    def test_self_transfer_is_recorded_once(self):
        self.edge(target=self.sender)
        sync_retained(self.graph,self.scope)
        report = self.reader().report(self.sender)
        self.assertEqual(len(report['trace_evidence']['self']),1)
        self.assertEqual(report['sources'][0]['revision'],1)

    def test_failed_dead_retracted_invalid_and_provider_only_rows_are_skipped(self):
        for overrides in ({'failed':True},{'dead_branch':True},{'retracted':True},
                          {'invalid_address':True},{'transaction_success':False},{'err':{'code':1}},
                          {'source':'core_arkm_funding'}):
            self.edge(**overrides)
        self.edge(sender='not-solana')
        self.edge(signature='invalid-signature')
        self.edge(source_sha256=None)
        self.edge(transaction_signature_verified=False)
        result = sync_retained(self.graph,self.scope)
        self.assertEqual(result['skipped'],11)
        self.assertEqual(result['projected'],0)
        self.assertEqual(self.reader().report(self.sender)['sources'],[])
        self.assertTrue(result['caught_up_to_retained_edges'])
        self.assertFalse(result['complete_wallet_history'])

    def test_invalid_and_oversized_json_remain_audited_without_blocking_later_rows(self):
        self.edge(body='{not-json')
        self.edge(body=canonical({'padding':'x'*65537}))
        self.edge()
        result = sync_retained(self.graph,self.scope)
        self.assertEqual(result['projected'],1)
        self.assertEqual(result['skip_reasons'],{'invalid_evidence_json':1,'oversized_evidence':1})
        rows = self.reader().db.execute('SELECT disposition FROM coverage_projection_edges ORDER BY graph_rowid').fetchall()
        self.assertEqual([r[0] for r in rows],['skipped','skipped','projected'])

    def test_bounded_catch_up_and_repeat_do_not_duplicate_or_rewrite_live_frontier(self):
        for offset in range(3):
            self.edge(slot=400000000+offset)
        with self.graph.db:
            self.graph.db.execute('INSERT INTO frontier VALUES(?,?)',('live-job','{"cursor":17}'))
        first = sync_retained(self.graph,self.scope,limit=1)
        self.assertEqual(first['last_graph_rowid'],1)
        self.assertFalse(first['caught_up_to_retained_edges'])
        second = sync_retained(self.graph,self.scope,limit=2)
        self.assertEqual(second['projected'],2)
        third = sync_retained(self.graph,self.scope)
        self.assertEqual(third['scanned'],0)
        reader = self.reader()
        self.assertEqual(reader.db.execute('SELECT count(*) FROM coverage_links').fetchone()[0],6)
        self.assertEqual(reader.db.execute('SELECT count(*) FROM coverage_projection_edges').fetchone()[0],3)
        self.assertEqual(self.graph.db.execute('SELECT body FROM frontier').fetchone()[0],'{"cursor":17}')

    def test_per_edge_failure_rolls_back_endpoints_and_preserves_checkpoint_for_retry(self):
        self.edge(slot=400000000)
        self.edge(slot=400000001)
        original = Coverage.record_link
        def fail_second(receiver,*args,**kwargs):
            if args[4]==400000001:
                raise ValueError('INJECTED_PROJECTION_FAILURE')
            return original(receiver,*args,**kwargs)
        with patch.object(Coverage,'record_link',fail_second):
            with self.assertRaisesRegex(ValueError,'INJECTED_PROJECTION_FAILURE'):
                sync_retained(self.graph,self.scope)
        reader = self.reader()
        self.assertEqual(reader.db.execute('SELECT last_rowid FROM coverage_projection_cursor').fetchone()[0],1)
        self.assertEqual(reader.db.execute('SELECT count(*) FROM coverage_links').fetchone()[0],2)
        self.assertEqual(reader.report(self.sender)['latest_observed_slot'],400000000)
        result = sync_retained(self.graph,self.scope)
        self.assertEqual(result['projected'],1)
        self.assertEqual(reader.db.execute('SELECT count(*) FROM coverage_links').fetchone()[0],4)
        self.assertEqual(reader.report(self.sender)['latest_observed_slot'],400000001)

    def test_changed_checkpoint_fails_closed_without_advancing(self):
        self.edge()
        sync_retained(self.graph,self.scope)
        with self.graph.db:
            self.graph.db.execute("UPDATE edges SET body=json_set(body,'$.retracted',1) WHERE rowid=1")
        self.edge(slot=400000001)
        with self.assertRaisesRegex(ValueError,'CHECKPOINT_CHANGED'):
            sync_retained(self.graph,self.scope)
        self.assertEqual(self.reader().db.execute('SELECT last_rowid FROM coverage_projection_cursor').fetchone()[0],1)

    def test_calendar_time_enrichment_does_not_rewrite_observed_slot(self):
        self.edge()
        sync_retained(self.graph,self.scope)
        with self.graph.db:
            self.graph.db.execute('UPDATE edges SET stamp=1792055000000 WHERE rowid=1')
        self.assertEqual(sync_retained(self.graph,self.scope)['scanned'],0)

    def test_provenance_metadata_is_pinned_and_query_only_projection_has_no_network(self):
        self.edge()
        sync_retained(self.graph,self.scope)
        reader = self.reader()
        metadata = reader.db.execute('SELECT body FROM coverage_projection_meta WHERE id=1').fetchone()[0]
        self.assertIn('append_only_retained_transaction_observations',metadata)
        self.assertIn('"whole_block_inspected":false',metadata)
        with sqlite3.connect(self.graph.out/'coverage.sqlite3') as db:
            db.execute("UPDATE coverage_projection_meta SET body='{}' WHERE id=1")
        with self.assertRaisesRegex(ValueError,'CORPUS_CHANGED'):
            sync_retained(self.graph,self.scope)

    def test_original_ticking_evidence_reference_supported_without_raw_upgrade(self):
        self.edge(source='ticking_retained',source_sha256=None,evidence='retained-frame-1',
                  finality='filtered_processed_stream')
        result = sync_retained(self.graph,self.scope)
        self.assertEqual(result['tiers'],{'ticking_processed_observation':1})
        refs = self.reader().report(self.sender)['latest_observation_refs']
        self.assertIn('original_evidence:retained-frame-1',refs)

    def test_legacy_and_rejected_commit_records_do_not_become_movements(self):
        pending=self.edge(ix=65537)
        rejected=self.edge(ix=65538)
        stored=self.edge(instruction_commit_status='legacy_unchecked')
        self.verdicts[pending]=dict(instruction_commit_status='legacy_unchecked',assessment_current=False)
        self.verdicts[rejected]=dict(instruction_commit_status='rejected',assessment_current=True)
        result=sync_retained(self.graph,self.scope)
        self.assertEqual(result['projected'],0)
        self.assertEqual(result['skip_reasons'],dict(instruction_commit_pending=1,
            instruction_commit_rejected=1,stored_instruction_commit_inadmissible=1))
        self.assertEqual(self.reader().report(self.sender)['sources'],[])

    def test_later_assessment_reviews_skipped_old_edge_without_resetting_high_water(self):
        edge=self.edge(ix=65537)
        self.verdicts[edge]=dict(instruction_commit_status='legacy_unchecked',assessment_current=False,revision='old')
        sync_retained(self.graph,self.scope)
        with self.graph.db:
            self.graph.db.execute('CREATE TABLE edge_commit_assessments(id INTEGER PRIMARY KEY,edge TEXT)')
            self.graph.db.execute('INSERT INTO edge_commit_assessments VALUES(?,?)',(1,edge))
        self.verdicts[edge]=dict(instruction_commit_status='committed',assessment_current=True,revision='new')
        reviewed=sync_retained(self.graph,self.scope)
        self.assertEqual(reviewed['last_graph_rowid'],1)
        self.assertEqual(reviewed['reviewed_assessments'],1)
        self.assertEqual(reviewed['review_projected'],1)
        reader=self.reader()
        self.assertEqual(reader.db.execute('SELECT disposition FROM coverage_projection_edges').fetchone()[0],'skipped')
        self.assertEqual(reader.db.execute('SELECT count(*) FROM coverage_projection_versions').fetchone()[0],2)
        self.assertEqual(len(report_retained(self.graph,self.sender)['trace_evidence']['outgoing']),1)
        repeated=sync_retained(self.graph,self.scope)
        self.assertEqual(repeated['reviewed_assessments'],0)
        self.assertEqual(reader.report(self.sender)['sources'][0]['revision'],1)

    def test_read_through_masks_retraction_behind_checkpoint_and_keeps_receipts(self):
        first=self.edge(slot=400000002)
        self.edge(slot=400000001)
        sync_retained(self.graph,self.scope)
        with self.graph.db:
            self.graph.db.execute("UPDATE edges SET body=json_set(body,'$.retracted',1) WHERE id=?",(first,))
        # The changed first edge is behind the rowid checkpoint. Current API
        # reads must still suppress it without deleting historical evidence.
        report=report_retained(self.graph,self.sender)
        self.assertEqual(report['latest_observed_slot'],400000001)
        self.assertEqual(len(report['trace_evidence']['outgoing']),1)
        self.assertTrue(report['current_evidence_view']['retraction_or_commit_review_pending'])
        self.assertEqual(self.reader().db.execute('SELECT count(*) FROM coverage_links').fetchone()[0],4)

    def test_read_through_hides_stale_commit_assessments_until_revalidated(self):
        edge=self.edge()
        sync_retained(self.graph,self.scope)
        self.verdicts[edge]=dict(instruction_commit_status='legacy_unchecked',assessment_current=False,revision='changed')
        report=report_retained(self.graph,self.sender)
        self.assertEqual(report['trace_evidence']['outgoing'],[])
        self.assertEqual(report['spans']['inspected'],[])
        self.assertIsNone(report['latest_observed_slot'])
        self.assertTrue(report['current_evidence_view']['historical_receipts_preserved'])
        self.assertEqual(self.reader().report(self.sender)['latest_observed_slot'],400000000)

    def test_account_creation_funding_instruction_kind_stays_separate(self):
        self.edge(transfer_kind='account_creation_funding')
        sync_retained(self.graph,self.scope)
        self.assertEqual(report_retained(self.graph,self.sender)['trace_evidence']['outgoing'][0]['relation'],
                         'account_creation_funding')

    def test_old_raw_edge_uses_matched_assessment_kind_without_rewriting_source_body(self):
        edge=self.edge(kind='raw_archive_transfer',transfer_kind=None,source=None,source_sha256=None)
        before=self.graph.db.execute('SELECT body FROM edges WHERE id=?',(edge,)).fetchone()[0]
        self.verdicts[edge]=dict(instruction_commit_status='committed',assessment_current=True,
            transaction_success=True,source_sha256='c'*64,transfer_kind='account_creation_funding',
            token_account_not_owner=False,revision='verified-old-raw')
        sync_retained(self.graph,self.scope)
        report=report_retained(self.graph,self.sender)
        self.assertEqual(report['trace_evidence']['outgoing'][0]['relation'],'account_creation_funding')
        self.assertEqual(self.graph.db.execute('SELECT body FROM edges WHERE id=?',(edge,)).fetchone()[0],before)

    def test_real_commit_assessment_read_join_invalidates_stale_view(self):
        from economic_machine.pump_research.commit_graph import latest_assessments, _revision
        edge=self.edge()
        body=self.graph.db.execute('SELECT body FROM edges WHERE id=?',(edge,)).fetchone()[0]
        index_path=self.graph.out/'address-index.sqlite3'
        with sqlite3.connect(index_path) as index:
            index.executescript('''CREATE TABLE tx_commit_evidence(tx INTEGER,version INTEGER,body TEXT);
                CREATE TABLE transactions(id INTEGER PRIMARY KEY,successful INTEGER);
                CREATE TABLE blocks(id INTEGER PRIMARY KEY,state TEXT);''')
            revision=_revision(index)[0]
        assessment=dict(edge=edge,edge_body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            status='committed',instruction_commit_status='committed',assessment_current=True,
            assessment_revision=revision,transaction_success=True,source_sha256='a'*64)
        with self.graph.db:
            self.graph.db.execute('CREATE TABLE edge_commit_assessments(id INTEGER PRIMARY KEY,edge TEXT,body TEXT)')
            self.graph.db.execute('CREATE TABLE edge_commit_checkpoints(id INTEGER PRIMARY KEY,revision TEXT)')
            self.graph.db.execute('INSERT INTO edge_commit_assessments VALUES(?,?,?)',(1,edge,canonical(assessment)))
            self.graph.db.execute('INSERT INTO edge_commit_checkpoints VALUES(?,?)',(1,revision))
        with patch('economic_machine.pump_research.coverage_projection._assessments',
                   side_effect=lambda graph,ids:latest_assessments(graph,ids,require_current=True)):
            sync_retained(self.graph,self.scope)
            self.assertEqual(len(report_retained(self.graph,self.sender)['trace_evidence']['outgoing']),1)
            # Change the real index before a new graph audit checkpoint exists.
            # Current reads must not trust the old checkpoint's cached revision.
            with sqlite3.connect(index_path) as index:
                index.execute('INSERT INTO tx_commit_evidence VALUES(?,?,?)',(1,1,'{}'))
            current=report_retained(self.graph,self.sender)
            self.assertIsNone(current['latest_observed_slot'])
            self.assertEqual(current['trace_evidence']['outgoing'],[])

    def test_transaction_active_invalid_limit_and_deadline_do_not_start_projection(self):
        for limit in (0,301,True):
            with self.assertRaisesRegex(ValueError,'PROJECTION_LIMIT'):
                sync_retained(self.graph,self.scope,limit=limit)
        self.graph.db.execute('BEGIN')
        try:
            with self.assertRaisesRegex(ValueError,'GRAPH_TRANSACTION_ACTIVE'):
                sync_retained(self.graph,self.scope)
        finally:
            self.graph.db.rollback()
        with patch('economic_machine.pump_research.coverage.time.time_ns',return_value=DEADLINE_MS*1000000):
            with self.assertRaisesRegex(ValueError,'DEADLINE'):
                sync_retained(self.graph,self.scope)
        self.assertFalse((self.graph.out/'coverage.sqlite3').exists())

    def test_writer_is_reused_without_holding_transaction_and_closed_with_graph(self):
        from economic_machine.pump_research.wallet_map import Graph
        self.edge()
        sync_retained(self.graph,self.scope)
        first=self.graph._coverage_projection_writer
        self.assertFalse(first.db.in_transaction)
        self.edge(slot=400000001)
        sync_retained(self.graph,self.scope)
        self.assertIs(self.graph._coverage_projection_writer,first)
        self.assertFalse(first.db.in_transaction)
        self.assertEqual(first.db.execute('SELECT count(*) FROM coverage_links').fetchone()[0],4)
        Graph.close(self.graph)
        self.assertIsNone(self.graph._coverage_projection_writer)
        with self.assertRaises(sqlite3.ProgrammingError):
            first.db.execute('SELECT 1')
        close_retained(self.graph)

    def test_failed_batch_keeps_sidecars_but_releases_write_transaction(self):
        self.edge()
        with patch.object(Coverage,'record_link',side_effect=ValueError('INJECTED')):
            with self.assertRaisesRegex(ValueError,'INJECTED'):
                sync_retained(self.graph,self.scope)
        writer=self.graph._coverage_projection_writer
        self.assertFalse(writer.db.in_transaction)
        self.assertEqual(writer.db.execute('SELECT count(*) FROM coverage_links').fetchone()[0],0)
        self.assertTrue(Path(str(writer.path)+'-wal').exists())
        self.assertTrue(Path(str(writer.path)+'-shm').exists())
        self.assertEqual(sync_retained(self.graph,self.scope)['projected'],1)
        self.assertIs(self.graph._coverage_projection_writer,writer)

    def test_reused_writer_still_enforces_current_scope_and_deadline(self):
        self.edge()
        sync_retained(self.graph,self.scope)
        writer=self.graph._coverage_projection_writer
        self.edge(slot=400000001)
        denied=SimpleNamespace(guard=lambda:(_ for _ in ()).throw(ValueError('CURRENT_SCOPE_DENIED')))
        with self.assertRaisesRegex(ValueError,'CURRENT_SCOPE_DENIED'):
            sync_retained(self.graph,denied)
        with patch('economic_machine.pump_research.coverage.time.time_ns',return_value=DEADLINE_MS*1000000):
            with self.assertRaisesRegex(ValueError,'COVERAGE_DEADLINE'):
                sync_retained(self.graph,self.scope)
        self.assertEqual(writer.db.execute('SELECT last_rowid FROM coverage_projection_cursor').fetchone()[0],1)
        self.assertFalse(writer.db.in_transaction)

    @unittest.skipUnless(hasattr(os,'geteuid'),'POSIX filesystem permissions required')
    def test_read_only_process_reads_live_wal_without_directory_write_access(self):
        self.edge()
        sync_retained(self.graph,self.scope)
        path=self.graph.out/'coverage.sqlite3'
        files=[path,Path(str(path)+'-wal'),Path(str(path)+'-shm')]
        self.assertTrue(all(p.exists() for p in files))
        modes={p:p.stat().st_mode & 0o777 for p in [self.graph.out,*files]}
        for p in files:p.chmod(0o444)
        self.graph.out.chmod(0o555)
        script='''import os,sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1])
try:
    fd=os.open(p.parent/'forbidden-write',os.O_WRONLY|os.O_CREAT,0o600)
except PermissionError:
    pass
else:
    os.close(fd)
    raise AssertionError('directory unexpectedly writable')
db=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
try:
    db.execute('PRAGMA query_only=ON')
    assert db.execute('SELECT count(*) FROM coverage_links').fetchone()[0]==2
    try:
        db.execute('CREATE TABLE forbidden(value)')
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError('read-only connection accepted a write')
finally:
    db.close()
print('read-only-wal-ok')
'''
        def unprivileged():
            if os.geteuid()==0:
                os.setgroups([]);os.setgid(65534);os.setuid(65534)
        try:
            completed=subprocess.run([str(Path(sys.executable).resolve()),'-c',script,str(path)],
                text=True,capture_output=True,timeout=10,preexec_fn=unprivileged)
            self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertEqual(completed.stdout.strip(),'read-only-wal-ok')
            self.assertTrue(all(p.exists() for p in files))
        finally:
            for p,mode in modes.items():p.chmod(mode)


if __name__=='__main__':
    unittest.main()
