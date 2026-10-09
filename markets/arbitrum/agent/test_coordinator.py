import pathlib,unittest
from coordinator import NativeLane,Profile,Frame
LIB=pathlib.Path(__file__).resolve().parents[1]/'build/libmachine_arbitrum.so'
def profile(a):return Profile(421614,a,1,10**12,0,8,2,3,1,30,1)
def frame(inventory,nonce=0,seq=1):return Frame(421614,seq+1,seq+1,seq,nonce,10000,1000,990,1010,inventory,0,0,0,0,0,0,1)
class NativeChecks(unittest.TestCase):
 def test_cohort_crossing_prices_bound_outward_without_changing_quantity(self):
  lane=NativeLane(LIB,[profile(1),profile(2)],1)
  try:
   payload,members=lane.propose({1:frame(8),2:frame(-8)})
   self.assertEqual(members,{1:1,2:1});self.assertEqual(lane.cohort_guards,1)
   first=int.from_bytes(payload[72:104],'big');second=int.from_bytes(payload[108:140],'big')
   self.assertEqual((first>>168)&0xffffffff,2);self.assertEqual((second>>136)&0xffffffff,2)
   self.assertLess((second>>104)&65535,(first>>120)&65535)
   self.assertGreaterEqual((first>>120)&65535,995);self.assertLessEqual((second>>104)&65535,1005)
   lane.ack(members,True)
  finally:lane.close()
 def test_pending_frame_and_bad_ack_never_consume_nonce(self):
  lane=NativeLane(LIB,[profile(1)],1)
  try:
   _,members=lane.propose({1:frame(0)})
   self.assertEqual(lane.propose({1:frame(0)})[0],b'')
   with self.assertRaises(ValueError):lane.ack({1:2},True)
   lane.ack(members,False)
   self.assertEqual(lane.propose({1:frame(0,seq=2)})[1],{1:1})
  finally:lane.close()
 def test_revoked_epoch_and_incomplete_frame_reject(self):
  lane=NativeLane(LIB,[profile(1)],1)
  try:
   with self.assertRaises(ValueError):lane.propose({})
   f=frame(0);f.session_epoch=2
   with self.assertRaises(ValueError):lane.propose({1:f})
  finally:lane.close()
if __name__=='__main__':unittest.main(verbosity=2)
