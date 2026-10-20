import hashlib
import concurrent.futures
import time
import unittest
from economic_machine.pump_research.memory_cache import VerifiedCache, capacity, GIB


class MemoryCacheTests(unittest.TestCase):
    def test_spare_ram_aggressive_with_reserve(self):
        self.assertEqual(capacity(0, 32*GIB, 40*GIB, 48*GIB), 32*GIB*4//5)
        self.assertEqual(capacity(12*GIB, 32*GIB, 6*GIB, 48*GIB), 10*GIB)
        self.assertEqual(capacity(GIB, 32*GIB, 40*GIB, 48*GIB, 4*GIB, 5*GIB), 2*GIB-256*1024**2)

    def test_repeated_reads_use_verified_bytes_and_namespace(self):
        cache=VerifiedCache(16, lambda current, limit: limit)
        raw=b'abcdefgh'; digest=hashlib.sha256(raw).hexdigest(); calls=[]
        def load(): calls.append(1); return raw
        self.assertEqual(cache.read('one',digest,8,load),raw)
        self.assertEqual(cache.read('one',digest,8,load),raw)
        cache.read('two',digest,8,load)
        self.assertEqual(len(calls),2)
        self.assertEqual(cache.snapshot()['avoided_verified_bytes'],8)

    def test_corruption_is_not_admitted(self):
        cache=VerifiedCache(16,lambda c,l:l)
        with self.assertRaisesRegex(ValueError,'HASH'):
            cache.read('x','0'*64,3,lambda:b'bad')
        self.assertEqual(cache.snapshot()['objects'],0)

    def test_pressure_reclaims_idle_cache_without_source_deletion(self):
        allowed=[16]; cache=VerifiedCache(16,lambda c,l:allowed[0])
        for raw in (b'aaaaaaaa',b'bbbbbbbb'):
            cache.read('x',hashlib.sha256(raw).hexdigest(),8,lambda:raw)
        allowed[0]=0;cache.trim()
        self.assertEqual(cache.snapshot()['resident_verified_bytes'],0)
        self.assertEqual(cache.snapshot()['evictions'],2)

    def test_lru_keeps_recent_objects(self):
        cache=VerifiedCache(16,lambda c,l:l)
        def get(raw):return cache.read('x',hashlib.sha256(raw).hexdigest(),8,lambda:raw)
        get(b'aaaaaaaa');get(b'bbbbbbbb');get(b'aaaaaaaa');get(b'cccccccc')
        self.assertEqual(cache.snapshot()['evictions'],1)
        loads=cache.loads;get(b'aaaaaaaa');self.assertEqual(cache.loads,loads)
        get(b'bbbbbbbb');self.assertEqual(cache.loads,loads+1)

    def test_parallel_same_object_loads_once(self):
        cache=VerifiedCache(16,lambda c,l:l); raw=b'abcdefgh';calls=[]
        def load():calls.append(1);time.sleep(.02);return raw
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:cache.read('x',hashlib.sha256(raw).hexdigest(),8,load),range(8)))
        self.assertEqual(results,[raw]*8);self.assertEqual(len(calls),1)

    def test_large_object_returns_but_does_not_pin(self):
        cache=VerifiedCache(2,lambda c,l:l);raw=b'abcdefgh'
        self.assertEqual(cache.read('x',hashlib.sha256(raw).hexdigest(),8,lambda:raw),raw)
        self.assertEqual(cache.bytes,0)


if __name__=='__main__':unittest.main()
