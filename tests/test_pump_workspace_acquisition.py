import json
from pathlib import Path
import tempfile
import time
import unittest

from economic_machine.pump_research.research_workspace import Workspace
from economic_machine.pump_research.store import canonical


class WorkspaceAcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name)
        (self.base/'evidence/wallet-map-v1').mkdir(parents=True)
        self.folder=self.base/'evidence/address-epochs-v2';self.folder.mkdir()
        self.path=self.folder/'status.json'
        self.workspace=Workspace(self.base);self.addCleanup(self.workspace.close)

    def receipt(self,**changes):
        value=dict(schema='economic-machine.epoch-acquisition-status.v2',
            at_ms=time.time_ns()//1000000,states={'IMPORTED':2,'LEGACY_IMPORT_REUSED':1,'QUEUED':1055,'DOWNLOADING':1},
            imported_epochs=2,legacy_imports_reused=1,catalogued_epochs=1059,
            pending_epochs=[1009,1008],pending_reason=None,
            active_epochs=[dict(epoch=1009,state='DOWNLOADING',received_bytes=2147483648,total_bytes=55320000000)])
        value.update(changes);self.path.write_text(canonical(value));return value

    def summary(self):return self.workspace.snapshot()['coverage']['acquisition']

    def test_download_is_separate_from_existing_and_new_searchable_indexes(self):
        self.receipt();before=self.path.read_bytes();value=self.summary()
        self.assertEqual(value['ready_index_count'],3)
        self.assertEqual(value['new_ready_index_count'],2)
        self.assertEqual(value['legacy_ready_index_count'],1)
        self.assertEqual(value['queued_epoch_count'],1055)
        self.assertEqual(value['current'],dict(epoch=1009,state='DOWNLOADING',searchable=False,
            received_bytes=2147483648,total_bytes=55320000000))
        self.assertFalse(value['downloaded_is_searchable'])
        self.assertFalse(value['complete_solana_history'])
        self.assertFalse(value['independent_consensus_verified'])
        self.assertEqual(self.path.read_bytes(),before)

    def test_downloaded_archive_stays_unsearchable_until_import_receipt_state(self):
        self.receipt(active_epochs=[dict(epoch=1009,state='DOWNLOADED_PENDING_INSPECTION',compressed_bytes=55320000000)])
        value=self.summary()
        self.assertEqual(value['ready_index_count'],3)
        self.assertFalse(value['current']['searchable'])
        self.assertEqual(value['current']['compressed_bytes'],55320000000)

    def test_missing_status_is_unavailable_and_does_not_invent_zero_indexes(self):
        value=self.summary()
        self.assertFalse(value['available'])
        self.assertNotIn('ready_index_count',value)
        self.assertFalse(self.path.exists())

    def test_malformed_oversized_and_symlink_status_are_unavailable(self):
        self.path.write_text('{');self.assertFalse(self.summary()['available'])
        self.path.write_text(' '*65537);self.assertEqual(self.summary()['reason'],'status_bound')
        self.path.unlink();target=self.folder/'other.json';target.write_text('{}')
        self.path.symlink_to(target);self.assertFalse(self.summary()['available'])

    def test_only_safe_summary_fields_leave_status_receipt(self):
        self.receipt(grant_sha256='private-reference',pending_reason='internal diagnostic details',
            network_url='private-network-endpoint',active_epochs=[dict(epoch=1009,state='DOWNLOADING',
                received_bytes=5,total_bytes=10,credential='private-credential')])
        value=self.summary();encoded=json.dumps(value)
        for hidden in ('private-reference','internal diagnostic details','private-network-endpoint','private-credential'):
            self.assertNotIn(hidden,encoded)
        self.assertTrue(value['needs_attention'])

    def test_invalid_and_conflicting_counters_remain_unknown(self):
        self.receipt(imported_epochs=True,legacy_imports_reused=8)
        value=self.summary()
        self.assertIsNone(value['ready_index_count'])
        self.assertIsNone(value['new_ready_index_count'])
        self.assertIsNone(value['legacy_ready_index_count'])

    def test_invalid_byte_progress_is_not_exposed_as_valid_progress(self):
        self.receipt(active_epochs=[dict(epoch=1009,state='DOWNLOADING',received_bytes=11,total_bytes=10)])
        value=self.summary()['current']
        self.assertNotIn('received_bytes',value);self.assertNotIn('total_bytes',value)
        self.assertFalse(value['searchable'])

    def test_stale_progress_is_retained_but_marked_not_current(self):
        self.receipt(at_ms=time.time_ns()//1000000-900001)
        value=self.summary()
        self.assertTrue(value['stale'])
        self.assertEqual(value['current']['epoch'],1009)

    def test_no_active_epoch_does_not_fabricate_download(self):
        self.receipt(active_epochs=[],pending_epochs=[])
        value=self.summary()
        self.assertIsNone(value['current'])
        self.assertEqual(value['pending_epoch_count'],0)
        self.assertEqual(value['ready_index_count'],3)


if __name__=='__main__':unittest.main()
