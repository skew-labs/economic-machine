import copy
import unittest
from unittest.mock import patch

import prepare_datapass_launch as launch

from economic_machine.values import MachineError

OWNER = "0x" + "1" * 40


class LaunchReview(unittest.TestCase):
    def fake(self, _url, method, params):
        self.calls.append(method)
        values = {"eth_chainId": "0xa4b1", "eth_getTransactionCount": "0x0", "eth_getBalance": "0x0",
                  "eth_call": hex(27125448), "eth_gasPrice": hex(20000000), "eth_estimateGas": hex(5600000)}
        return values[method]

    def test_zero_eth_review_is_quoted_but_not_marked_funded(self):
        self.calls = []
        with patch.object(launch, "rpc", self.fake): result = launch.prepare(OWNER, 500000000000000)
        self.assertEqual(result["status"], "NEEDS_NATIVE_ETH")
        self.assertEqual(result["broadcasts"], 0)
        self.assertEqual(result["token"]["initial_supply"], "0")
        self.assertLessEqual(int(result["maximum_gas_wei"]), 500000000000000)
        self.assertNotIn("eth_sendRawTransaction", self.calls)

    def test_budget_and_wrong_chain_are_rejected(self):
        self.calls = []
        with self.assertRaises(MachineError): launch.prepare(OWNER, 500000000000001)
        with patch.object(launch, "rpc", self.fake), self.assertRaises(MachineError): launch.prepare(OWNER, 1)
        with patch.object(launch, "rpc", return_value="0x1"), self.assertRaises(MachineError):
            launch.prepare(OWNER, 500000000000000)

    def test_changed_compiled_source_rejected_before_rpc(self):
        contracts, manifest = launch.compiled()
        self.assertIn("SkewLaunchBundle", contracts)
        self.assertIn("SkewArtifactMining.sol", manifest["sources"])
        bad = copy.deepcopy(manifest)
        bad["sources"]["SkewArtifactMining.sol"] = "0" * 64
        with patch.object(launch.json, "loads", return_value=bad), self.assertRaises(MachineError):
            launch.compiled()

class PrivateRpcErrors(unittest.TestCase):
    def test_rpc_transport_never_exposes_credential_url(self):
        import httpx
        from unittest.mock import patch
        from machine_commerce import datapass
        secret_url = 'https://rpc.invalid/secret-credential'
        with patch.object(datapass, '_rpc_read', side_effect=httpx.ConnectError(secret_url)):
            with self.assertRaisesRegex(Exception, '^RPC_READ_UNAVAILABLE$'):
                datapass.rpc_read(secret_url, 'eth_call', [])
        with patch.object(launch, '_rpc', side_effect=httpx.ConnectError(secret_url)):
            with self.assertRaisesRegex(Exception, '^RPC_READ_FAILED$'):
                launch.rpc(secret_url, 'eth_call', [])

class FinalizedStateReconciliation(unittest.TestCase):
    def test_pruned_deployment_state_uses_one_pinned_finalized_block(self):
        import hashlib
        from eth_abi import encode
        names=['SkewLaunchBundle','SkewDataPass','SkewArtifactMining','SkewSolutionToken']
        addresses={name:'0x'+str(i+2)*40 for i,name in enumerate(names)}
        artifact={name:{'runtime':'6000','immutable_references':{}} for name in names}
        review={'owner':OWNER,'transaction':{'data':'0x6001','nonce':'0x1'},'initcode_sha256':hashlib.sha256(bytes.fromhex('6001')).hexdigest(),'owner_cap_wei':'500000000000000'}
        selectors={}
        def value(target,sig,kind,result):selectors[(target,launch.call_data(sig,[],[]))]='0x'+encode([kind],[result]).hex()
        bundle,passport,mining,token=(addresses[name] for name in names)
        for sig,target in [('dataPass()',passport),('mining()',mining),('token()',token)]:value(bundle,sig,'address',target)
        for target,sig,result in [(passport,'owner()',OWNER),(mining,'publisher()',OWNER),(mining,'dataPass()',passport),(mining,'rewardToken()',token),(token,'mining()',mining)]:value(target,sig,'address',result)
        value(token,'totalSupply()','uint256',0);value(token,'cap()','uint256',160000*10**18)
        calls=[]
        def rpc(url,method,params):
            calls.append((url,method,params))
            if method=='eth_chainId':return '0xa4b1'
            if method=='eth_getTransactionReceipt':return {'status':'0x1','blockNumber':'0x10','blockHash':'0x'+'1'*64,'contractAddress':bundle,'gasUsed':'0x1','effectiveGasPrice':'0x1'}
            if method=='eth_getTransactionByHash':return {'from':OWNER,'to':None,'input':'0x6001','value':'0x0','nonce':'0x1'}
            if method=='eth_getBlockByNumber':
                n=('0x20' if url==launch.NETWORKS[42161]['rpcs'][0] else '0x21') if params[0]=='finalized' else params[0]
                return {'number':n,'hash':'0x'+('1' if n=='0x10' else '2')*64}
            if method in {'eth_call','eth_getCode'}:
                self.assertEqual(params[-1],'0x20','Do not require archived deployment state')
                if method=='eth_getCode':return '0x6000'
                return selectors[(params[0]['to'],params[0]['data'])]
            raise AssertionError(method)
        with patch.object(launch,'compiled',return_value=(artifact,{})),patch.object(launch,'rpc',rpc):
            result=launch.reconcile(review,'0x'+'a'*64)
        self.assertEqual(result['status'],'MAINNET_DEPLOYED_FINALIZED')
        self.assertTrue(all(x['state_block_number']==32 for x in result['observations']))
        self.assertTrue(all(not m.startswith('eth_send') for _,m,_ in calls))
        original=rpc
        def inconsistent(url,method,params):
            result=original(url,method,params)
            if method=='eth_getBlockByNumber' and params[0]=='0x20' and url==launch.NETWORKS[42161]['rpcs'][1]:
                result['hash']='0x'+'3'*64
            return result
        with patch.object(launch,'compiled',return_value=(artifact,{})),patch.object(launch,'rpc',inconsistent),self.assertRaisesRegex(MachineError,'RPC_DEPLOYMENT_DISAGREEMENT'):
            launch.reconcile(review,'0x'+'a'*64)
