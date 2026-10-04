import importlib.util
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('mm_observer',Path(__file__).parents[1]/'scripts/metamask-agent-status.py')
observer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


class ObserverTests(unittest.TestCase):
    def test_only_three_read_commands_and_allowlisted_output(self):
        calls=[]
        def read(node,cli,args):
            calls.append(args)
            return {'doctor':{'authenticated':True,'initialized':True,'private':'secret'},
                'init':{'walletMode':'server-wallet','tradingMode':'guard'},
                'wallet':{'address':'0x'+'1'*40,'private':'secret'}}[args[0]]
        result=observer.observe('node','cli',reader=read,clock=lambda:1000)
        self.assertTrue(result['authenticated'])
        self.assertEqual(calls,[['doctor'],['init','show'],['wallet','address','--chain-namespace','evm']])
        self.assertNotIn('private',result)

    def test_no_wallet_calls_before_authentication_and_initialization(self):
        for data in [{'authenticated':False,'initialized':True},{'authenticated':True,'initialized':False}]:
            calls=[]
            def read(node,cli,args):calls.append(args);return data
            self.assertFalse(observer.observe('node','cli',reader=read)['authenticated'])
            self.assertEqual(calls,[['doctor']])

    def test_network_failure_replaces_readiness_with_unauthenticated(self):
        def failed(*args):raise OSError('sensitive service detail')
        result=observer.observe('node','cli',reader=failed,clock=lambda:1000)
        self.assertFalse(result['authenticated']); self.assertIsNone(result['address'])
        self.assertNotIn('sensitive',str(result))

    def test_beast_mode_never_admitted(self):
        def read(node,cli,args):
            if args==['doctor']:return {'authenticated':True,'initialized':True}
            if args==['init','show']:return {'walletMode':'server-wallet','tradingMode':'beast'}
            self.fail('wallet must not be read outside guard mode')
        self.assertFalse(observer.observe('node','cli',reader=read)['authenticated'])
