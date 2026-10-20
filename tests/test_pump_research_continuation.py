import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from economic_machine.muse_maker import MODEL
from economic_machine.pump_research.research_continuation import (
    AUTHORIZATION,CATALOG,DENIED,FOLDER,GIB,MAX_REQUEST,PARENTS,
    ContinuationScope,authority,build_grant,
)
from economic_machine.pump_research.store import canonical


def make_continuation_fixture(base,now_ms=None,legacy_deadline_ms=None,publish=True):
    """Full valid legacy chain for parent integration tests; no live authority."""
    base=Path(base);now=time.time_ns()//1000000 if now_ms is None else now_ms
    deadline=now-1000 if legacy_deadline_ms is None else legacy_deadline_ms
    pins={};documents={}
    def write(key,document):
        path=base/PARENTS[key];path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(canonical(document)+'\n');digest=hashlib.sha256(path.read_bytes()).hexdigest()
        documents[key]=document;pins[key]=digest
        if key not in ('parent_grant_sha256','original_model_scope_sha256'):
            path.with_suffix('.sha256').write_text(digest+'\n')
        return digest
    parent=write('parent_grant_sha256',dict(schema='economic-machine.pump-core-grant.v1',
        additional_io_bytes=12*GIB,total_storage_bytes=32*GIB,maximum_seconds=43200,
        maximum_cpu_percent=100,trading=False,paid_api=False,new_model_calls=0,new_server=False,
        approved_at_ms=deadline-10000,deadline_ms=deadline))
    common=dict(parent_grant_sha256=parent,deadline_ms=deadline)
    epoch=write('previous_epoch_grant_sha256',dict(common,schema='economic-machine.address-epoch-grant.v1',
        additional_io_bytes=32*GIB,total_storage_bytes=96*GIB,maximum_cpu_percent=100,epochs=[685],
        paid_api=False,trading=False,new_server=False))
    model=write('original_model_scope_sha256',dict(authorization='owner_unlimited_muse_top10_forward_wallet_research',
        model=MODEL,deadline_ms=deadline,maximum_usd=None,max_calls=None,trade_authority=False,
        io_and_storage_grant_sha256=parent))
    denied={k:False for k in DENIED}
    internal=write('previous_internal_grant_sha256',dict(common,
        authorization='owner_private_solana_research_ten_muse_workers',parallel_models=10,model=MODEL,
        original_model_scope_sha256=model,**denied))
    write('previous_parallel_grant_sha256',dict(common,schema='economic-machine.parallel-research-scope.v1',
        authorization='owner_private_solana_research_hundred_muse_workers',parallel_models=100,model=MODEL,
        supersedes_internal_grant_sha256=internal,original_model_scope_sha256=model,
        source_fetch_parallelism=1,evidence_writer_parallelism=1,maximum_aggregate_cpu_percent=100,
        total_storage_bytes=224*GIB,**denied))
    write('previous_expansion_grant_sha256',dict(common,schema='economic-machine.solana-history-expansion.v2',
        authorization='owner_all_public_solana_history_hundred_muse_workers',approved_ms=deadline-5000,
        additional_io_bytes=128*GIB,total_storage_bytes=224*GIB,minimum_host_free_bytes=128*GIB,
        maximum_aggregate_cpu_percent=100,epoch_scope='all_public_solana_history',priority_epochs=[1009,1008],
        original_ledgers_preserved=True,previous_epoch_grant_sha256=epoch,previous_internal_grant_sha256=internal,**denied))
    folder=base/'evidence/address-epochs-v2';folder.mkdir()
    raw=b'| 685 | available |\n| 1008 | available |\n| 1009 | ongoing |\n'
    digest=hashlib.sha256(raw).hexdigest();name=f'catalogue-{digest}.md'
    (folder/name).write_bytes(raw)
    (folder/'catalogue-receipt.json').write_text(canonical(dict(path=name,sha256=digest,bytes=len(raw),url=CATALOG)))
    folder=base/FOLDER;folder.mkdir()
    grant=build_grant(base,now)
    if publish:
        path=folder/'grant.json';path.write_text(canonical(grant)+'\n')
        path.with_suffix('.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'\n')
    return SimpleNamespace(base=base,now=now,deadline=deadline,pins=pins,documents=documents,grant=grant,folder=folder)


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name)
        fixture=make_continuation_fixture(self.base,publish=False)
        self.now,self.deadline=fixture.now,fixture.deadline
        self.documents=fixture.documents;self.grant=fixture.grant;self.folder=fixture.folder
        self.parent=fixture.pins['parent_grant_sha256'];self.expansion=fixture.pins['previous_expansion_grant_sha256']
        self.disk=patch('economic_machine.pump_research.research_continuation.shutil.disk_usage',
            return_value=SimpleNamespace(free=400*GIB));self.disk.start();self.addCleanup(self.disk.stop)
        self.storage=patch('economic_machine.pump_research.history_core.used_bytes',return_value=40*GIB)
        self.storage.start();self.addCleanup(self.storage.stop)

    def write(self,grant=None):
        path=self.folder/'grant.json';path.write_text(canonical(grant or self.grant)+'\n')
        path.with_suffix('.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'\n')

    def scope(self):
        self.write();scope=ContinuationScope(self.base,self.parent,self.deadline)
        self.addCleanup(scope.close);return scope

    def test_missing_grant_is_not_implicit_continuation(self):
        self.assertIsNone(authority(self.base))
        with self.assertRaisesRegex(ValueError,'MISSING'):ContinuationScope(self.base)

    def test_builder_only_returns_document_and_keeps_all_parent_files(self):
        before={name:(self.base/name).read_bytes() for name in PARENTS.values()}
        value=build_grant(self.base,self.now)
        self.assertEqual(value['authorization'],AUTHORIZATION)
        self.assertEqual(value['inventory_epochs'],[685,1008,1009])
        self.assertFalse((self.folder/'grant.json').exists())
        self.assertEqual(before,{name:(self.base/name).read_bytes() for name in PARENTS.values()})

    def test_original_deadline_is_retained_but_new_pinned_operation_has_no_stop_time(self):
        scope=self.scope();scope.guard();scope.reserve(11,'free-source-public-record')
        self.assertIsNone(scope.deadline);self.assertIsNone(scope.effective_deadline_ms)
        self.assertEqual(scope.legacy_deadline_ms,self.deadline)
        self.assertEqual(scope.parent_expansion_hash,self.expansion)
        self.assertIsNone(scope.remaining_io())
        self.assertEqual(scope.reserved(),11)
        self.assertEqual(json.loads((self.base/PARENTS['parent_grant_sha256']).read_bytes())['deadline_ms'],self.deadline)

    def test_new_ledger_does_not_reset_or_reuse_legacy_reservations(self):
        old=self.base/'evidence/solana-expansion-v2/io.sqlite3'
        db=sqlite3.connect(old);db.execute('CREATE TABLE reservations(bytes INTEGER)')
        db.execute('INSERT INTO reservations VALUES(?)',(128*GIB,));db.commit();db.close()
        old_bytes=old.read_bytes();scope=self.scope()
        # A large retained amount exercises removal of the cumulative cap;
        # each actual new admission still enforces MAX_REQUEST.
        with scope.db:scope.db.execute('INSERT INTO reservations(bytes,purpose,at_ms) VALUES(?,?,?)',(512*GIB,'retained-fixture',self.now))
        scope.reserve(17,'new-free-source')
        self.assertEqual(scope.reserved(),512*GIB+17)
        self.assertEqual(old.read_bytes(),old_bytes)

    def test_inventory_is_frozen_and_nonmember_slots_are_rejected(self):
        scope=self.scope();scope.assert_epoch(685);scope.assert_slot(1009*432000)
        self.assertEqual(scope.maximum_slot,1010*432000-1)
        for epoch in (True,684,686,1010):
            with self.assertRaisesRegex(ValueError,'EPOCH_OUTSIDE'):scope.assert_epoch(epoch)
        for slot in (-1,True,686*432000,1010*432000):
            with self.assertRaises(ValueError):scope.assert_slot(slot)

    def test_changed_catalogue_or_inventory_requires_new_authority(self):
        self.write();path=self.base/'evidence/address-epochs-v2/catalogue-receipt.json'
        original=path.read_bytes();path.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError,'AUTHORITY'):authority(self.base)
        path.write_bytes(original)
        value=dict(self.grant,inventory_epochs=[685,1008,1009,1010]);self.write(value)
        with self.assertRaisesRegex(ValueError,'AUTHORITY'):authority(self.base)

    def test_parent_changes_and_pin_fail_closed(self):
        self.write();path=self.base/PARENTS['previous_parallel_grant_sha256'];path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'PARENT_PIN'):authority(self.base)

    def test_strict_fields_cannot_widen_model_resources_or_permissions(self):
        self.write()
        edits=[('parallel_models',101),('parallel_models',100.0),('source_fetch_parallelism',True),
            ('model','another-model'),('deadline_ms',self.now+1),('additional_io_bytes',0),
            ('total_storage_bytes',225*GIB),('minimum_host_free_bytes',0),('maximum_aggregate_cpu_percent',101),
            ('max_request_bytes',MAX_REQUEST+1),('extend_inventory_automatically',True)]
        edits.extend((flag,True) for flag in DENIED)
        for field,value in edits:
            with self.subTest(field=field,value=value):
                changed=dict(self.grant);changed[field]=value;self.write(changed)
                with self.assertRaisesRegex(ValueError,'AUTHORITY'):authority(self.base)

    def test_omitted_null_field_is_not_explicit_uncapped_authority(self):
        for name in ('deadline_ms','additional_io_bytes'):
            changed=dict(self.grant);del changed[name];self.write(changed)
            with self.assertRaisesRegex(ValueError,'AUTHORITY'):authority(self.base)

    def test_repinning_grant_cannot_reset_existing_ledger_identity(self):
        self.write();scope=ContinuationScope(self.base);scope.reserve(13,'retained');scope.close()
        changed=dict(self.grant,approved_ms=self.now-1);self.write(changed)
        with self.assertRaisesRegex(ValueError,'AUTHORITY_CHANGED'):ContinuationScope(self.base)
        db=sqlite3.connect(self.folder/'io.sqlite3')
        try:self.assertEqual(db.execute('SELECT sum(bytes) FROM reservations').fetchone()[0],13)
        finally:db.close()

    def test_per_request_limit_and_purpose_still_apply(self):
        scope=self.scope()
        for size in (0,-1,True,MAX_REQUEST+1):
            with self.assertRaisesRegex(ValueError,'REQUEST_BOUND'):scope.reserve(size,'bad')
        for purpose in ('','x'*513,None):
            with self.assertRaisesRegex(ValueError,'PURPOSE'):scope.reserve(1,purpose)
        self.assertEqual(scope.reserved(),0)

    def test_storage_and_host_free_guards_block_new_reservations(self):
        scope=self.scope()
        with patch('economic_machine.pump_research.history_core.used_bytes',return_value=224*GIB):
            with self.assertRaisesRegex(ValueError,'TOTAL_STORAGE'):scope.reserve(1,'blocked')
        with patch('economic_machine.pump_research.research_continuation.shutil.disk_usage',return_value=SimpleNamespace(free=128*GIB)):
            with self.assertRaisesRegex(ValueError,'HOST_FREE'):scope.reserve(1,'blocked')
        self.assertEqual(scope.reserved(),0)

    def test_space_guard_is_rechecked_after_writer_lock(self):
        scope=self.scope()
        with patch.object(scope,'guard',side_effect=[None,ValueError('CORE_HOST_FREE_SPACE')]) as guard:
            with self.assertRaisesRegex(ValueError,'HOST_FREE'):scope.reserve(1,'waiting')
        self.assertEqual(guard.call_count,2);self.assertEqual(scope.reserved(),0)
        self.assertFalse(scope.db.in_transaction)

    def test_multiple_clients_preserve_every_additive_reservation(self):
        self.write()
        def reserve(number):
            scope=ContinuationScope(self.base)
            try:return scope.reserve(number,'client-'+str(number))
            finally:scope.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            ids=list(pool.map(reserve,[11,13,17]))
        self.assertEqual(len(set(ids)),3)
        scope=ContinuationScope(self.base);self.addCleanup(scope.close)
        self.assertEqual(scope.reserved(),41)


if __name__=='__main__':unittest.main()
