"""Continuation crosses the old deadline without resetting original evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_pump_research_continuation import make_continuation_fixture
from test_pump_parallel_workspace import GraphCase, response
from economic_machine.pump_research.gsfa_index import Budget as IndexBudget
from economic_machine.pump_research.history_core import Scope
from economic_machine.pump_research.research_parallel import load_parallel_authority
from economic_machine.pump_research.research_continuation import GIB, PARENTS
from economic_machine.pump_research.store import canonical
from economic_machine.pump_research.wallet_map import MODEL, UncappedMuse, select_parallel


class ContinuedRuntimeTests(GraphCase):
    def setUp(self):
        super().setUp()
        self.fixture=make_continuation_fixture(self.base)
        disk=patch('economic_machine.pump_research.research_continuation.shutil.disk_usage',
            return_value=SimpleNamespace(free=400*GIB))
        disk.start();self.addCleanup(disk.stop)
        self.originals={p:(self.base/p).read_bytes() for p in PARENTS.values()}
        self.scope=Scope(self.base,self.base/PARENTS['parent_grant_sha256'])
        self.addCleanup(self.scope.close)
        self.model_scope=self.fixture.documents['original_model_scope_sha256']

    def budget(self,continued=True):
        result=UncappedMuse(self.graph.out/'test-model.sqlite3',self.model_scope,
            self.scope.continuation if continued else None)
        self.addCleanup(result.db.close);return result

    def test_core_and_index_guards_continue_but_keep_parent_deadline_and_bytes(self):
        self.assertLess(self.scope.grant['deadline_ms'],time.time_ns()//1000000)
        self.scope.guard();self.assertIsNone(self.scope.effective_deadline_ms)
        IndexBudget(None,1024,self.scope.guard).consume(32)
        self.scope.reserve(11,'continued-map')
        self.scope.expansion.reserve(17,'continued-index')
        self.assertEqual(self.scope.continuation.reserved(),28)
        self.assertEqual(self.scope.expansion.db.execute('select count(*) from reservations').fetchone()[0],0)
        self.assertEqual(self.scope.db.execute('select count(*) from reservations').fetchone()[0],0)
        self.assertIsNone(self.scope.remaining_io())
        self.assertEqual(self.originals,{p:(self.base/p).read_bytes() for p in PARENTS.values()})

    def test_model_scope_row_remains_original_when_new_call_is_reserved(self):
        budget=self.budget()
        req=dict(model=MODEL,max_tokens=4096,messages=[])
        ident=budget.reserve(req,time.time_ns()//1000000)
        self.assertEqual(ident,1)
        self.assertEqual(budget.db.execute('select body from scope where id=1').fetchone()[0],canonical(self.model_scope))
        self.assertEqual(budget.scope['deadline_ms'],self.fixture.deadline)
        self.assertIsNone(budget.effective_deadline_ms)

    def test_missing_continuation_still_enforces_model_deadline(self):
        with self.assertRaisesRegex(ValueError,'MAP_RESEARCH_DEADLINE'):
            self.budget(False).reserve(dict(model=MODEL,max_tokens=4096,messages=[]),time.time_ns()//1000000)

    def test_parallel_child_validates_original_grants_after_old_deadline(self):
        result=load_parallel_authority(self.graph,self.scope)
        self.assertIsNone(result['deadline_ms'])
        self.assertEqual(result['original_deadline_ms'],self.fixture.deadline)
        self.assertEqual(result['continuation_sha256'],self.scope.continuation.hash)
        self.assertEqual(result['parallel_models'],100)

    def test_provider_lanes_run_after_old_deadline_only_with_matching_continuation(self):
        root=self.root()
        for _ in range(4):self.job(root)
        budget=self.budget()
        with patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=response_call) as provider:
            jobs=select_parallel(self.graph,budget,'not-read',self.scope,workers=2)
        self.assertEqual(provider.call_count,2);self.assertEqual(len(jobs),2)
        self.assertEqual(budget.totals()['accepted_responses'],2)

    def test_mismatched_model_continuation_never_calls_provider(self):
        root=self.root();self.job(root)
        with patch('economic_machine.pump_research.wallet_map.provider_call') as provider:
            with self.assertRaisesRegex(ValueError,'THREAD_CONTINUATION_BOUND'):
                select_parallel(self.graph,self.budget(False),'not-read',self.scope,workers=2)
        provider.assert_not_called()

    def test_no_fixed_deadline_does_not_disable_storage_guard(self):
        with patch('economic_machine.pump_research.history_core.used_bytes',return_value=224*GIB):
            with self.assertRaisesRegex(ValueError,'CORE_TOTAL_STORAGE_LIMIT'):
                self.scope.guard(force=True)

    def test_null_deadline_without_a_bound_continuation_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'GSFA_CONTINUATION_SCOPE_REQUIRED'):
            IndexBudget(None,1024,lambda *args:None)


def response_call(req,credential):return response(req)


if __name__=='__main__':unittest.main()
