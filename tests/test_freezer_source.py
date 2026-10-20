import struct
import unittest
from economic_machine.arbitrum_research.freezer_source import metadata,bounds,Snapshot,segments,table_prefix

class FreezerTests(unittest.TestCase):
 def test_table_location_legacy_and_nested(self):
  for path in ['./l2chaindata/ancient/headers.cidx','./l2chaindata/ancient/chain/headers.cidx']:
   self.assertEqual(table_prefix({path:{}}),path[:-len('/headers.cidx')])
 def test_table_location_rejects_ambiguity_and_unrelated_paths(self):
  for files in [{}, {'./foo/headers.cidx':{}}, {'./l2/ancient/headers.cidx':{},'./l2/ancient/chain/headers.cidx':{}}]:
   with self.assertRaisesRegex(ValueError,'LOCATION_PENDING'):table_prefix(files)
 def test_metadata_v1(self):self.assertEqual(metadata(bytes.fromhex('c20180')),dict(version=1,tail=0,flush=None))
 def test_metadata_v2(self):self.assertEqual(metadata(bytes.fromhex('c402808180')),dict(version=2,tail=0,flush=128))
 def test_metadata_rejects(self):
  for raw in [b'',b'\xc1\x02',b'\xc3\x02\x80\x80\x01',b'\xc3\x02\x00\x80',b'\xc4\x02\x80\x81\x01',b'\xc3\x03\x80\x80']:
   with self.subTest(raw=raw),self.assertRaises(ValueError):metadata(raw)
 def test_file_crossing(self):
  raw=b''.join(struct.pack('>HI',*x) for x in [(2,15),(2,20),(3,8)])
  self.assertEqual(bounds(raw),[(2,15,5),(3,0,8)])
 def test_removed_prefix(self):
  raw=struct.pack('>HIHI',7,1000,7,20);self.assertEqual(bounds(raw,first_item=True),[(7,0,20)])
 def test_bad_indices(self):
  for items in [[(2,20),(2,19)],[(2,0),(4,10)],[(2,0),(1,20)],[(0,0),(0,17*1024**2)]]:
   with self.subTest(items=items),self.assertRaises(ValueError):bounds(b''.join(struct.pack('>HI',*x) for x in items))
 def test_malformed_index(self):
  for raw in [b'',bytes(6),bytes(13)]:
   with self.subTest(n=len(raw)),self.assertRaises(ValueError):bounds(raw)
 def test_member_scope(self):
  s=Snapshot.__new__(Snapshot);s.prefix='x';s.files={'x/a':dict(bytes=4,data_offset=512,directory=False)};s.read=lambda o,n:(o,n)
  self.assertEqual(s.member('a',1,3),(513,3))
  for start,n in [(-1,1),(3,2),(0,-1)]:
   with self.assertRaises(ValueError):s.member('a',start,n)
 def test_missing_file(self):
  s=Snapshot.__new__(Snapshot);s.prefix='x';s.files={}
  with self.assertRaisesRegex(ValueError,'PENDING'):s.member('headers.0000.cdat',0,1)
 def test_large_member_crosses_multipart_boundary(self):
  rows=[dict(offset=0,bytes=3*1024**2),dict(offset=3*1024**2,bytes=4*1024**2)]
  self.assertEqual([(p['offset'],o,n) for p,o,n in segments(rows,1024**2,5*1024**2)],[(0,1024**2,2*1024**2),(3*1024**2,0,3*1024**2)])
  with self.assertRaises(ValueError):segments(rows,0,9*1024**2)
 def test_aligned_index_read_ahead_and_short_final_range(self):
  s=Snapshot.__new__(Snapshot);s.prefix='x';s.read_ahead=True
  data=bytes(range(256))*1100;s.files={'x/headers.cidx':dict(bytes=len(data),data_offset=512,directory=False)};calls=[]
  def read(offset,size):calls.append((offset,size));return data[offset-512:offset-512+size]
  s.read=read
  self.assertEqual(s.member('headers.cidx',262140,100),data[262140:262240])
  self.assertEqual(calls,[(512,262144),(262656,len(data)-262144)])
 def test_cached_read_obeys_deadline(self):
  s=Snapshot.__new__(Snapshot)
  def expired():raise ValueError('FREEZER_DEADLINE')
  s.guard=expired
  with self.assertRaisesRegex(ValueError,'DEADLINE'):s.read(0,1)

if __name__=='__main__':unittest.main()
