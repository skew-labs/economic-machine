import random,struct,sys,time,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'agents'),str(Path(__file__).resolve().parents[1]/'muse'),str(Path(__file__).resolve().parents[1]/'client')]
from compiled import Compiled
from compiled_service import local_features
from wire import SIZE,NODE,MAGIC,snapshot
from program import content_hash

def arena(seed):
    rng=random.Random(seed);d=bytearray(SIZE);d[:8]=MAGIC;d[216]=1
    now=int(time.time());struct.pack_into('<QQQ',d,144,15000+seed%50,1,seed+1);struct.pack_into('<Q',d,224,now)
    for s in range(3):
        a=512+s*256;d[a+168]=1;struct.pack_into('<Qq',d,a+112,1_000_000,rng.randrange(-7,8))
    for n in range(48):
        o=NODE+n*48;struct.pack_into('<QQI',d,o,rng.randrange(0,10),rng.choice([0,2]),rng.randrange(14970,15030));struct.pack_into('<Q',d,o+40,rng.choice([0,now-1,now+10]))
    return bytes(d)
def engine(role):
    p=[8,16,5000,20,3,1];return Compiled(role,10**18,{'parameters':p,'program_hash':content_hash(p),'version':1,'execution_authority':False},now_ns=1)
class CompactFeed(unittest.TestCase):
    def test_binary_decoder_matches_dictionary_path_and_negative_floor(self):
        for role in (0,1):
            a=engine(role);b=engine(role);previous=0
            try:
                for seed in range(250):
                    data=arena(seed);st=snapshot(data);features=local_features(st,1,previous);now=1000+seed*10
                    old=a.decide(st,1,now,features,now_ns=now+1)
                    new,decoded,mark=b.decide_bytes(data,1,now,previous,now_ns=now+1)
                    self.assertEqual(features,decoded)
                    for k in ('quotes','mode','reduce','position','center','execution_authority'):self.assertEqual(old[k],new[k],(seed,k))
                    a.checkpoint(old);b.checkpoint(new);previous=mark
            finally:a.close();b.close()
    def test_invalid_arena_and_oracle_cannot_produce_candidate(self):
        c=engine(0)
        try:
            for offset,value in ((0,0),(137,1),(216,0),(512+168,0)):
                d=bytearray(arena(0));d[offset]=value
                with self.assertRaises(RuntimeError):c.decide_bytes(bytes(d),1,100,now_ns=101)
            with self.assertRaises(RuntimeError):c.decide_bytes(arena(0)[:-1],1,100,now_ns=101)
            with self.assertRaises(RuntimeError):c.decide_bytes(arena(0),1,100,now_ns=101,wall=int(time.time())+91)
        finally:c.close()
if __name__=='__main__':unittest.main(verbosity=2)
