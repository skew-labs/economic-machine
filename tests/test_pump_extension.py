import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from economic_machine.pump_research.research_extension import extension,GIB
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'products/pump_research'))
from epoch_download import checked_range

class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.folder=self.base/'evidence/address-epoch-v1';self.folder.mkdir(parents=True)
        self.grant=dict(schema='economic-machine.address-epoch-grant.v1',parent_grant_sha256='abc',deadline_ms=1000,
            additional_io_bytes=32*GIB,total_storage_bytes=96*GIB,maximum_cpu_percent=100,epochs=[685],paid_api=False,trading=False,new_server=False)
    def write(self):
        raw=json.dumps(self.grant).encode();(self.folder/'grant.json').write_bytes(raw)
        (self.folder/'grant.sha256').write_text(hashlib.sha256(raw).hexdigest())
    def test_absent_keeps_original_scope(self):self.assertIsNone(extension(self.base,'abc',1000))
    def test_explicit_pinned_approval(self):
        self.write();self.assertEqual(extension(self.base,'abc',1000)['total_storage_bytes'],96*GIB)
    def test_no_implicit_deadline_extension(self):
        self.write()
        with self.assertRaises(ValueError):extension(self.base,'abc',999)
    def test_changed_parent_rejected(self):
        self.write()
        with self.assertRaises(ValueError):extension(self.base,'different',1000)
    def test_tampered_file_rejected(self):
        self.write();(self.folder/'grant.json').write_text(json.dumps(self.grant)+' ')
        with self.assertRaises(ValueError):extension(self.base,'abc',1000)
    def test_unapproved_spending_rejected(self):
        self.grant['paid_api']=True;self.write()
        with self.assertRaises(ValueError):extension(self.base,'abc',1000)
    def test_unapproved_epoch_rejected(self):
        self.grant['epochs']=[700];self.write()
        with self.assertRaises(ValueError):extension(self.base,'abc',1000)

class RangeTests(unittest.TestCase):
    def test_precise_range(self):self.assertEqual(checked_range('bytes 20-29/100',20,10,100),100)
    def test_shifted_response_rejected(self):
        with self.assertRaises(ValueError):checked_range('bytes 19-28/100',20,10)
    def test_object_size_change_rejected(self):
        with self.assertRaises(ValueError):checked_range('bytes 20-29/101',20,10,100)
    def test_short_total_rejected(self):
        with self.assertRaises(ValueError):checked_range('bytes 20-29/25',20,10)
    def test_missing_header_rejected(self):
        with self.assertRaises(ValueError):checked_range(None,20,10)

if __name__=='__main__':unittest.main()
