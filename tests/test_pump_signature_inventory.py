import struct
import unittest
from economic_machine.pump_research.signature_inventory import header,count_entries


ROOT=b'\x01\x71\x12\x20'+b'a'*32
def fixture():
    pairs={b'epoch':(1009).to_bytes(8,'little'),b'rootCid':ROOT,b'network':b'mainnet',b'kind':b'sig-to-cid'}
    meta=bytes([len(pairs)])+b''.join(bytes([len(k)])+k+bytes([len(v)])+v for k,v in pairs.items())
    raw=b'compiszd'+struct.pack('<I',13+len(meta))+struct.pack('<QI',36,2)+bytes([1])+meta
    start=len(raw)+32
    table=b''.join(struct.pack('<II',1,n)+bytes([3,0])+offset.to_bytes(6,'little') for n,offset in [(5,start),(7,start+5*39)])
    return raw,table,start+12*39


class Count(unittest.TestCase):
    def test_two_bucket_count(self):
        raw,t,size=fixture();h=header(raw,epoch=1009,root_cid=ROOT)
        result=count_entries(h,t,size)
        self.assertEqual(result['provider_signature_entries'],12)
        self.assertFalse(result['complete_canonical_history_proven'])

    def test_wrong_epoch(self):
        raw,_,_=fixture()
        with self.assertRaisesRegex(ValueError,'IDENTITY'):header(raw,epoch=0,root_cid=ROOT)

    def test_wrong_root(self):
        raw,_,_=fixture()
        with self.assertRaisesRegex(ValueError,'IDENTITY'):header(raw,epoch=1009,root_cid=b'x'*36)

    def test_legacy_format_rejected(self):
        raw,_,_=fixture()
        with self.assertRaisesRegex(ValueError,'MAGIC'):header(b'compindx'+raw[8:],epoch=1009,root_cid=ROOT)

    def test_header_truncation(self):
        raw,_,_=fixture()
        with self.assertRaisesRegex(ValueError,'BOUND'):header(raw[:-1],epoch=1009,root_cid=ROOT)

    def test_table_truncation(self):
        raw,t,size=fixture();h=header(raw,epoch=1009,root_cid=ROOT)
        with self.assertRaisesRegex(ValueError,'BOUND'):count_entries(h,t[:-1],size)

    def test_bucket_gap(self):
        raw,t,size=fixture();h=header(raw,epoch=1009,root_cid=ROOT)
        t=bytearray(t);t[10]+=1
        with self.assertRaisesRegex(ValueError,'LAYOUT'):count_entries(h,t,size)

    def test_trailing_unknown_data(self):
        raw,t,size=fixture();h=header(raw,epoch=1009,root_cid=ROOT)
        with self.assertRaisesRegex(ValueError,'FINAL_SIZE'):count_entries(h,t,size+1)


if __name__=='__main__':unittest.main()
