import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from economic_machine.pump_research.research_parallel import load_parallel_authority
from economic_machine.pump_research.store import canonical
from economic_machine.muse_maker import MODEL


class ParallelAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.out=Path(temporary.name)
        self.states={}
        self.graph=SimpleNamespace(out=self.out,state=lambda key:self.states.get(key))
        self.scope=SimpleNamespace(guard=lambda:None,hash='c'*64,storage_limit=96*1024**3,
            grant={'deadline_ms':1792078753482})
        model=dict(authorization='owner_unlimited_muse_top10_forward_wallet_research',
            maximum_usd=None,max_calls=None,model=MODEL,trade_authority=False,
            deadline_ms=self.scope.grant['deadline_ms'])
        model_hash=self.write('model-scope.json',model)
        legacy=dict(authorization='owner_private_solana_research_ten_muse_workers',
            parallel_models=10,model=MODEL,deadline_ms=self.scope.grant['deadline_ms'],
            parent_grant_sha256=self.scope.hash,original_model_scope_sha256=model_hash)
        legacy_hash=self.write('internal-grant.json',legacy,pin=True)
        self.grant=dict(schema='economic-machine.parallel-research-scope.v1',
            authorization='owner_private_solana_research_hundred_muse_workers',parallel_models=100,
            model=MODEL,parent_grant_sha256=self.scope.hash,supersedes_internal_grant_sha256=legacy_hash,
            original_model_scope_sha256=model_hash,deadline_ms=self.scope.grant['deadline_ms'],
            source_fetch_parallelism=1,evidence_writer_parallelism=1,maximum_aggregate_cpu_percent=100,
            total_storage_bytes=self.scope.storage_limit,trading=False,public_deployment=False,
            paid_data_api=False,new_servers=False,production_collector_changes=False)
        self.write('parallel-grant.json',self.grant,pin=True)

    def write(self,name,value,pin=False):
        path=self.out/name;path.write_text(canonical(value)+'\n')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if pin:path.with_suffix('.sha256').write_text(digest+'\n')
        return digest

    def read(self,**kwargs):
        return load_parallel_authority(self.graph,self.scope,now_ms=1792070000000,**kwargs)

    def test_pinned_additive_capacity_is_read_only_and_preserves_original_files(self):
        before={p.name:p.read_bytes() for p in self.out.iterdir()}
        result=self.read()
        self.assertEqual(result['parallel_models'],100)
        self.assertEqual(result['model'],MODEL)
        self.assertEqual(result['source_fetch_parallelism'],1)
        self.assertTrue(result['model_ledger_preserved'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.out.iterdir()})
        self.assertEqual(self.states,{})

    def test_missing_or_wrong_child_pin_never_raises_capacity(self):
        (self.out/'parallel-grant.sha256').write_text('f'*64)
        with self.assertRaisesRegex(ValueError,'MAP_PARALLEL_GRANT_HASH'):self.read()

    def test_changed_parent_or_model_files_fail_closed(self):
        model=json.loads((self.out/'model-scope.json').read_text());model['max_calls']=10
        self.write('model-scope.json',model)
        with self.assertRaisesRegex(ValueError,'MAP_PARALLEL_AUTHORITY'):self.read()

    def test_deadline_model_network_and_single_writer_bounds_are_enforced(self):
        changes=[('deadline_ms',self.grant['deadline_ms']+1),('model','other'),
            ('source_fetch_parallelism',100),('evidence_writer_parallelism',100),
            ('parallel_models',101),('trading',True),('paid_data_api',True),
            ('total_storage_bytes',self.scope.storage_limit+1)]
        for field,value in changes:
            with self.subTest(field=field):
                grant=dict(self.grant);grant[field]=value
                self.write('parallel-grant.json',grant,pin=True)
                with self.assertRaisesRegex(ValueError,'MAP_PARALLEL_AUTHORITY'):self.read()

    def test_expired_grant_and_changed_existing_hash_pin_fail(self):
        with self.assertRaisesRegex(ValueError,'MAP_PARALLEL_DEADLINE'):
            load_parallel_authority(self.graph,self.scope,now_ms=self.grant['deadline_ms'])
        self.states['parallel_authority_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'MAP_PARALLEL_AUTHORITY_CHANGED'):self.read()

    def test_explicit_child_path_uses_its_own_pin(self):
        directory=self.out/'solana-expansion-v2';directory.mkdir()
        self.write('solana-expansion-v2/grant.json',self.grant,pin=True)
        result=self.read(path=directory/'grant.json')
        self.assertEqual(result['parallel_models'],100)

    def test_scope_guard_failure_cannot_be_bypassed(self):
        self.scope.guard=lambda:(_ for _ in ()).throw(ValueError('ORIGINAL_SCOPE_EXPIRED'))
        with self.assertRaisesRegex(ValueError,'ORIGINAL_SCOPE_EXPIRED'):self.read()


if __name__=='__main__':
    unittest.main()
