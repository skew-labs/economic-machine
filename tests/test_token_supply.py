import asyncio
import copy
import hashlib
import unittest

from eth_utils import keccak
from machine_commerce.token_market import deployment
from machine_commerce.token_supply import TokenSupply, uint

NOW = 1791110000
BLOCK = '0x' + '1' * 64
CODE = '0x60016000'


class SupplyTests(unittest.TestCase):
    def fixture(self, mutation=None):
        publication = copy.deepcopy(deployment())
        publication['runtime_sha256']['SkewSolutionToken'] = hashlib.sha256(bytes.fromhex(CODE[2:])).hexdigest()
        calls = []
        async def rpc(url, method, params):
            calls.append((url, method, params))
            if method == 'eth_chainId':result = '0xa4b1'
            elif method == 'eth_getBlockByNumber':
                result = {'number':hex(100 if url == 'one' else 101) if params[0] == 'latest' else params[0],
                          'hash':BLOCK,'timestamp':hex(NOW-2)}
            elif method == 'eth_getCode':result = CODE
            elif method == 'eth_call':
                values = {'totalSupply()':7*10**18, 'cap()':160000*10**18, 'decimals()':18}
                selectors = {'0x'+keccak(text=k)[:4].hex():v for k,v in values.items()}
                result = '0x'+format(selectors[params[0]['data']], '064x')
            else:raise AssertionError('No writes permitted')
            return mutation(url,method,params,result) if mutation else result
        return publication, calls, rpc

    def test_supply_uses_common_block_and_singleflight_cache(self):
        publication,calls,rpc=self.fixture()
        async def run():
            feed=TokenSupply(rpc,lambda:NOW,('one','two'))
            rows=await asyncio.gather(*(feed.snapshot(publication) for _ in range(5)))
            self.assertEqual(rows[0]['total_supply'],'7')
            self.assertEqual(rows[0]['maximum_supply'],'160000')
            self.assertEqual(rows[0]['block_number'],100)
            self.assertEqual(len([c for c in calls if c[1]=='eth_call']),6)
            self.assertTrue(all(c[2][1]=='0x64' for c in calls if c[1]=='eth_call'))
        asyncio.run(run())

    def test_stale_wrong_chain_code_and_rpc_disagreement_fail_closed(self):
        def wrong_chain(u,m,p,r):return '0x1' if m=='eth_chainId' else r
        def stale(u,m,p,r):return dict(r,timestamp=hex(NOW-181)) if m=='eth_getBlockByNumber' else r
        def code(u,m,p,r):return '0x6002' if m=='eth_getCode' else r
        def fork(u,m,p,r):return dict(r,hash='0x'+'2'*64) if u=='two' and m=='eth_getBlockByNumber' else r
        for mutation in [wrong_chain,stale,code,fork]:
            with self.subTest(mutation=mutation.__name__):
                publication,_,rpc=self.fixture(mutation)
                value=asyncio.run(TokenSupply(rpc,lambda:NOW,('one','two')).snapshot(publication))
                self.assertEqual(value['status'],'UNAVAILABLE');self.assertIsNone(value['total_supply'])

    def test_failed_refresh_does_not_show_old_supply_as_live(self):
        publication,_,rpc=self.fixture();clock=[NOW]
        async def run():
            feed=TokenSupply(rpc,lambda:clock[0],('one','two'))
            self.assertEqual((await feed.snapshot(publication))['status'],'AVAILABLE')
            clock[0]+=181
            self.assertIsNone((await feed.snapshot(publication))['total_supply'])
        asyncio.run(run())

    def test_no_deployment_never_reads_rpc(self):
        async def forbidden(*args):raise AssertionError('unexpected call')
        self.assertIsNone(asyncio.run(TokenSupply(forbidden).snapshot(None))['total_supply'])

    def test_bad_rpc_numbers_rejected(self):
        for value in [True,None,'0x','0x-1','0x'+'f'*65,'NaN']:
            with self.assertRaises(ValueError):uint(value)
