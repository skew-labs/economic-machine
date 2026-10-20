import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from economic_machine.pump_research.research_expansion import ExpansionScope,authority,GIB,MAX_REQUEST
from economic_machine.pump_research.history_core import Scope


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.parent=self.base/'evidence/core-trace-v1/grant.json'
        self.parent.parent.mkdir(parents=True)
        now=time.time_ns()//1000000
        self.deadline=now+600000
        self.old=dict(schema='economic-machine.pump-core-grant.v1',additional_io_bytes=12*GIB,
            total_storage_bytes=32*GIB,maximum_seconds=43200,maximum_cpu_percent=100,
            trading=False,paid_api=False,new_model_calls=0,new_server=False,
            approved_at_ms=now,deadline_ms=self.deadline)
        self.parent.write_text(json.dumps(self.old))
        self.parent_hash=hashlib.sha256(self.parent.read_bytes()).hexdigest()
        epoch=self.base/'evidence/address-epoch-v1/grant.json';epoch.parent.mkdir(parents=True)
        e=dict(schema='economic-machine.address-epoch-grant.v1',parent_grant_sha256=self.parent_hash,
            deadline_ms=self.deadline,additional_io_bytes=32*GIB,total_storage_bytes=96*GIB,
            maximum_cpu_percent=100,epochs=[685],paid_api=False,trading=False,new_server=False)
        epoch.write_text(json.dumps(e));epoch.with_suffix('.sha256').write_text(hashlib.sha256(epoch.read_bytes()).hexdigest())
        internal=self.base/'evidence/wallet-map-v1/internal-grant.json';internal.parent.mkdir(parents=True)
        internal.write_text('{"parallel_models":10}')
        self.folder=self.base/'evidence/solana-expansion-v2';self.folder.mkdir(parents=True)
        self.g=dict(schema='economic-machine.solana-history-expansion.v2',
            authorization='owner_all_public_solana_history_hundred_muse_workers',
            parent_grant_sha256=self.parent_hash,deadline_ms=self.deadline,approved_ms=now,
            additional_io_bytes=128*GIB,total_storage_bytes=224*GIB,minimum_host_free_bytes=128*GIB,
            maximum_aggregate_cpu_percent=100,epoch_scope='all_public_solana_history',
            priority_epochs=[1009,1008],original_ledgers_preserved=True,
            previous_epoch_grant_sha256=hashlib.sha256(epoch.read_bytes()).hexdigest(),
            previous_internal_grant_sha256=hashlib.sha256(internal.read_bytes()).hexdigest(),
            trading=False,public_deployment=False,paid_data_api=False,new_servers=False,
            production_collector_changes=False)
        self.disk=patch('economic_machine.pump_research.research_expansion.shutil.disk_usage',
            return_value=type('Disk',(),{'free':300*GIB})())
        self.disk.start();self.addCleanup(self.disk.stop)
    def write(self):
        p=self.folder/'grant.json';p.write_text(json.dumps(self.g))
        p.with_suffix('.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest())
    def test_absent_preserves_legacy(self):
        self.assertIsNone(authority(self.base,self.parent_hash,self.deadline))
        s=Scope(self.base,self.parent);self.addCleanup(s.close)
        self.assertIsNone(s.expansion);self.assertEqual(s.storage_limit,96*GIB)
    def test_additive_shared_reservations_preserve_old_grants_and_rows(self):
        s=Scope(self.base,self.parent);s.reserve(11,'old');s.close()
        old=self.parent.read_bytes();self.write()
        s=Scope(self.base,self.parent);self.addCleanup(s.close)
        e=ExpansionScope(self.base);self.addCleanup(e.close)
        s.reserve(13,'new-map');e.reserve(17,'new-epoch')
        self.assertEqual(s.reserved(),41)
        self.assertEqual(s.db.execute('SELECT sum(bytes) FROM reservations').fetchone()[0],11)
        self.assertEqual(e.reserved(),30);self.assertEqual(s.remaining_io(),128*GIB-30)
        self.assertEqual(self.parent.read_bytes(),old)
        self.assertEqual(s.storage_limit,224*GIB)
    def test_concurrent_clients_have_one_aggregate_allowance(self):
        self.write();s=ExpansionScope(self.base)
        with s.db:s.db.execute('INSERT INTO reservations(bytes,purpose,at_ms) VALUES(?,?,?)',(128*GIB-MAX_REQUEST,'preexisting',0))
        s.close()
        def reserve(i):
            c=ExpansionScope(self.base)
            try:c.reserve(MAX_REQUEST,'client'+str(i));return True
            except ValueError as e:self.assertEqual(str(e),'CORE_ADDITIONAL_IO_LIMIT');return False
            finally:c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve,range(4))),1)
        s=ExpansionScope(self.base);self.addCleanup(s.close)
        self.assertEqual(s.reserved(),128*GIB)
    def test_repin_does_not_reset_ledger_identity(self):
        self.write();s=ExpansionScope(self.base);s.reserve(1,'old');s.close()
        self.g['approved_ms']-=1;self.write()
        with self.assertRaisesRegex(ValueError,'AUTHORITY_CHANGED'):ExpansionScope(self.base)
    def test_tampering_parent_and_deadline_rejected(self):
        self.write()
        with self.assertRaisesRegex(ValueError,'AUTHORITY'):ExpansionScope(self.base,'bad',self.deadline)
        self.g['deadline_ms']+=1;self.write()
        with self.assertRaisesRegex(ValueError,'AUTHORITY'):ExpansionScope(self.base)
    def test_previous_approval_files_must_remain_unchanged(self):
        self.write();p=self.base/'evidence/address-epoch-v1/grant.json';p.write_text(p.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'PREVIOUS_AUTHORITY'):ExpansionScope(self.base)
    def test_no_paid_or_live_scope(self):
        for k in ('paid_data_api','new_servers','trading','public_deployment','production_collector_changes'):
            self.g[k]=True;self.write()
            with self.assertRaisesRegex(ValueError,'AUTHORITY'):ExpansionScope(self.base)
            self.g[k]=False
    def test_deadline_and_space_guards(self):
        self.write();s=ExpansionScope(self.base);self.addCleanup(s.close)
        with patch('time.time_ns',return_value=self.deadline*1000000):
            with self.assertRaisesRegex(ValueError,'DEADLINE'):s.reserve(1,'late')
        with patch('economic_machine.pump_research.history_core.used_bytes',return_value=224*GIB):
            with self.assertRaisesRegex(ValueError,'TOTAL_STORAGE'):s.guard(force=True)
        with patch('economic_machine.pump_research.history_core.used_bytes',return_value=1):
            with patch('economic_machine.pump_research.research_expansion.shutil.disk_usage',return_value=type('D',(),{'free':128*GIB})()):
                with self.assertRaisesRegex(ValueError,'HOST_FREE'):s.guard(1,True)
    def test_request_bounds(self):
        self.write();s=ExpansionScope(self.base);self.addCleanup(s.close)
        for value in (True,0,-1,MAX_REQUEST+1):
            with self.assertRaisesRegex(ValueError,'REQUEST_BOUND'):s.reserve(value,'bad')
        self.assertEqual(s.reserved(),0)

    def test_admission_expired_while_waiting_for_writer(self):
        self.write();s=ExpansionScope(self.base);self.addCleanup(s.close)
        # The second guard executes with the database writer lock held.
        with patch.object(s,'guard',side_effect=[None,ValueError('CORE_DEADLINE')]) as guard:
            with self.assertRaisesRegex(ValueError,'DEADLINE'):s.reserve(1,'late-lock')
        self.assertEqual(guard.call_count,2)
        self.assertFalse(s.db.in_transaction)
        self.assertEqual(s.reserved(),0)


if __name__=='__main__':unittest.main()
