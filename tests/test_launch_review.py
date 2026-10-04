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
