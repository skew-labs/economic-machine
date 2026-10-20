import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak
from test_atlas import NOW, sample_report

from economic_machine.values import MachineError
from machine_commerce.datapass import (
    CHAIN_ID,
    RPCS,
    USDC,
    DataPassChain,
    DataProducts,
    ReleaseMissing,
    bytes32,
    release_identity,
)

BUYER = "0x" + "1" * 40
SELLER = "0x" + "2" * 40
CONTRACT = "0x" + "3" * 40
CODE = "0x60016001"
CODE_HASH = hashlib.sha256(bytes.fromhex(CODE[2:])).hexdigest()


class Reader:
    def __init__(self):
        self.calls = []; self.chain = CHAIN_ID; self.hashes = ["0x" + "a" * 64] * 2
        self.expiry = NOW + 100; self.accepted = True; self.offset = 0
        self.price = 10000; self.content = sample_report()["report_sha256"]
        self.wrong_release = False
        self.purchase_token = 0
        self.asset = USDC
        self.release_missing = False
    def __call__(self, url, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId": return hex(self.chain)
        if method == "eth_getBlockByNumber":
            return {"number": "0x100", "hash": self.hashes[RPCS.index(url)], "timestamp": hex(NOW - 30 + self.offset)}
        if method == "eth_getCode": return CODE
        if method == "eth_call":
            selector = params[0]["data"][2:10]
            if selector == keccak(text="purchaseIds(address,bytes32)")[:4].hex():
                return "0x" + encode(["uint256"], [self.purchase_token]).hex()
            if selector == keccak(text="publishers(address)")[:4].hex():
                return "0x" + encode(["bool"], [self.accepted]).hex()
            if selector == keccak(text="entitlement(uint256,address,bytes32,bytes32)")[:4].hex():
                return "0x" + encode(["bool"], [self.accepted]).hex()
            if selector == keccak(text="licenses(uint256)")[:4].hex():
                report = sample_report()
                release = b"a" * 32 if self.wrong_release else release_identity(report["report_sha256"], report["terms_sha256"])
                return "0x" + encode(["bytes32", "uint64"], [release, self.expiry]).hex()
            if selector == keccak(text="sales(uint256)")[:4].hex():
                return "0x" + encode(["address", "uint128", "uint64", "uint64"], [SELLER, self.price, NOW + 90, 3]).hex()
            report = sample_report()
            if self.release_missing:
                raise ReleaseMissing("RELEASE_NOT_REGISTERED")
            return "0x" + encode(["(address,address,bytes32,bytes32,bytes32,uint128,uint64,uint64,bool,bool,string)"],
                [(SELLER, self.asset, bytes32(self.content), bytes32(report["terms_sha256"]),
                  bytes32(report["derived"]["source_observation_root"]), self.price, 86400,
                  NOW + 100, True, True, "https://example.com/report")]).hex()
        raise AssertionError(method)


class DataPassReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader = Reader()
        self.chain = DataPassChain(CONTRACT, CODE_HASH, self.reader, lambda: NOW)
        self.report = sample_report()

    def verify(self):
        return self.chain.entitled(1, BUYER, self.report["report_sha256"], self.report["terms_sha256"])

    def test_delivery_requires_both_pinned_rpc_and_runtime(self):
        proof = self.verify()
        self.assertTrue(proof["accepted"])
        self.assertEqual(proof["evidence"]["sources"], ["ARBITRUM_PRIMARY", "ARBITRUM_VERIFIER"])
        self.assertEqual(proof["evidence"]["block_number"], 256)
        self.assertFalse(any(method.startswith("eth_send") for method, _ in self.reader.calls))
        self.assertEqual(proof["evidence"]["calls_at_same_block"], 2)
        self.assertEqual(len(self.reader.calls), 12)

    def test_finality_lag_does_not_extend_expired_license(self):
        self.reader.expiry = NOW
        self.assertFalse(self.verify()["accepted"])
        self.reader.expiry = NOW - 1
        self.assertFalse(self.verify()["accepted"])

    def test_two_rpc_disagreement_blocks_access(self):
        self.reader.hashes[1] = "0x" + "b" * 64
        with self.assertRaisesRegex(MachineError, "DISAGREEMENT"): self.verify()

    def test_wrong_chain_runtime_future_and_stale_block_fail_closed(self):
        self.reader.chain = 1
        with self.assertRaisesRegex(MachineError, "WRONG_CHAIN"): self.verify()
        self.reader.chain = CHAIN_ID
        self.chain.code_hash = "f" * 64
        with self.assertRaisesRegex(MachineError, "RUNTIME_MISMATCH"): self.verify()
        self.chain.code_hash = CODE_HASH
        for offset in [-4000, 100]:
            self.reader.offset = offset
            with self.subTest(offset=offset), self.assertRaisesRegex(MachineError, "STALE_ENTITLEMENT"): self.verify()

    def test_unsigned_exact_purchase_binds_version_terms_and_price(self):
        plan = self.chain.purchase_plan(BUYER, self.report, "a" * 64)
        self.assertEqual(plan["amount_atoms"], "10000")
        self.assertEqual(plan["transactions"][0]["to"].lower(), USDC)
        self.assertEqual(plan["transactions"][1]["to"].lower(), CONTRACT)
        self.assertEqual(plan["broadcasts"], 0)
        self.assertFalse(plan["x402_payment_required"])
        self.reader.content = "f" * 64
        with self.assertRaisesRegex(MachineError, "TERMS_MISMATCH"):
            self.chain.purchase_plan(BUYER, self.report, "a" * 64)

    def test_price_cap_self_sale_and_invalid_token_fail_closed(self):
        self.reader.price = 1000001
        with self.assertRaises(MachineError): self.chain.purchase_plan(BUYER, self.report, "a" * 64)
        self.reader.price = 10000
        with self.assertRaises(MachineError): self.chain.purchase_plan(SELLER, self.report, "a" * 64)
        for token in [True, -1, 2**256, "01", "1.0", "0x1"]:
            with self.subTest(token=token), self.assertRaises(MachineError):
                self.chain.entitled(token, BUYER, "a" * 64, "b" * 64)

    def test_alternate_release_with_same_content_does_not_grant_canonical_access(self):
        self.reader.wrong_release = True
        self.assertFalse(self.verify()["accepted"])

    def test_resale_plan_binds_nonce_price_and_remaining_original_license(self):
        plan = self.chain.resale_plan(BUYER, self.report, "1", "a" * 64, 60)
        self.assertEqual(plan["listing_nonce"], 3)
        self.assertEqual(plan["price_atoms"], "10000")
        self.assertEqual(plan["license_expires"], NOW + 100)
        self.assertEqual(plan["transactions"][1]["purpose"], "ATOMIC_LICENSE_RESALE")
        self.assertEqual(plan["broadcasts"], 0)
        with self.assertRaises(MachineError): self.chain.resale_plan(BUYER, self.report, 1, "a" * 64, 600)
        self.reader.asset = CONTRACT
        with self.assertRaisesRegex(MachineError, "ASSET_OR_TRANSFER"):
            self.chain.resale_plan(BUYER, self.report, 1, "a" * 64, 60)

    def test_unconfigured_contract_cannot_claim_a_deployment(self):
        chain = DataPassChain()
        self.assertEqual(chain.status()["status"], "NOT_DEPLOYED")
        with self.assertRaisesRegex(MachineError, "NOT_DEPLOYED"):
            chain.entitled(1, BUYER, "a" * 64, "b" * 64)

    def test_registration_plan_matches_the_actual_compiled_contract_abi(self):
        from web3 import Web3
        root = Path(__file__).resolve().parents[1]
        abi = json.loads((root / "artifacts/contracts.json").read_text())["SkewDataPass"]["abi"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "atlas.json"; path.write_text(json.dumps(self.report))
            service = DataProducts(path, self.chain)
            self.reader.release_missing = True
            plan = service.registration({"address": SELLER, "chain_id": CHAIN_ID}, 10000, 86400)
            function, args = Web3().eth.contract(abi=abi).decode_function_input(plan["data"])
            self.assertEqual(function.fn_name, "registerRelease")
            self.assertEqual(args["release"]["price"], 10000)
            self.assertEqual(args["release"]["contentRoot"], bytes32(self.report["report_sha256"]))
            self.assertEqual(args["release"]["seller"].lower(), SELLER)
            self.reader.accepted = False
            with self.assertRaisesRegex(MachineError, "PUBLISHER"):
                service.registration({"address": SELLER, "chain_id": CHAIN_ID}, 10000, 86400)

    def test_publication_requires_missing_release_on_both_verified_rpc(self):
        self.reader.release_missing = True
        result = self.chain.release_status(self.report)
        self.assertEqual(result["status"], "NOT_REGISTERED")
        self.assertEqual(result["evidence"]["finality"], "TWO_RPC_FINALIZED_L2")
        original = self.reader
        def disputed(url, method, params):
            original.release_missing = url == RPCS[0]
            return original(url, method, params)
        self.chain.reader = disputed
        with self.assertRaisesRegex(MachineError, "DISAGREEMENT"):
            self.chain.release_status(self.report)

    def test_registered_release_cannot_be_republished(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "atlas.json"; path.write_text(json.dumps(self.report))
            with self.assertRaisesRegex(MachineError, "ALREADY_REGISTERED"):
                DataProducts(path, self.chain).registration({"address": SELLER, "chain_id": CHAIN_ID},10000,86400)

    def test_rpc_outage_is_not_an_unregistered_release(self):
        def failed(*args): raise MachineError("RPC_READ_UNAVAILABLE")
        self.chain.reader = failed
        with self.assertRaisesRegex(MachineError, "RPC_READ_UNAVAILABLE"):
            self.chain.release_status(self.report)

    def test_rpc_wrapper_retains_missing_release_and_wraps_transport_failure(self):
        from machine_commerce.datapass import rpc_read
        with patch("machine_commerce.datapass._rpc_read", side_effect=ReleaseMissing("RELEASE_NOT_REGISTERED")):
            with self.assertRaises(ReleaseMissing): rpc_read(RPCS[0], "eth_call", [])
        with patch("machine_commerce.datapass._rpc_read", side_effect=ValueError("bad response")):
            with self.assertRaisesRegex(MachineError, "RPC_READ_UNAVAILABLE"): rpc_read(RPCS[0], "eth_call", [])

    def test_unavailable_result_cannot_bypass_entitlement(self):
        self.reader.release_missing = True
        with self.assertRaisesRegex(MachineError, "ONLY_FOR_RELEASE"):
            self.chain.read_many(["0x12345678"], allow_missing_release=True)

    def test_historical_version_is_retained_when_current_release_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "atlas.json"; path.write_text(json.dumps(self.report))
            versions = path.parent / "versions"; versions.mkdir()
            (versions / (self.report["report_sha256"] + ".json")).write_text(json.dumps(self.report))
            service = DataProducts(path, self.chain)
            path.write_text("{}")
            self.assertEqual(service.version(self.report["report_sha256"]), self.report)
            with self.assertRaises(MachineError): service.version("../release")


class PurchaseStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reader = Reader()
        self.report = sample_report()
        path = Path(self.tmp.name) / "atlas.json"
        path.write_text(json.dumps(self.report))
        chain = DataPassChain(CONTRACT, CODE_HASH, self.reader, lambda: NOW)
        self.service = DataProducts(path, chain)
        self.identity = {"address": BUYER, "chain_id": CHAIN_ID}

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_finalized_purchase_does_not_authorize_payment_retry(self):
        result = self.service.purchase_status(self.identity, "a" * 64)
        self.assertEqual(result["status"], "NOT_FINALIZED")
        self.assertFalse(result["safe_to_retry_payment"])
        calls = [params for method, params in self.reader.calls if method == "eth_call"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(params[0]["data"].endswith(BUYER[2:].rjust(64, "0") + "a" * 64) for params in calls))
        self.assertEqual(result["evidence"]["sources"], ["ARBITRUM_PRIMARY", "ARBITRUM_VERIFIER"])

    def test_finalized_purchase_still_requires_owned_unexpired_release_before_delivery(self):
        self.reader.purchase_token = 7
        result = self.service.purchase_status(self.identity, "a" * 64)
        self.assertEqual(result["token_id"], "7")
        self.assertEqual(result["delivery"]["artifact_sha256"], self.report["report_sha256"])
        self.reader.expiry = NOW
        with self.assertRaises(PermissionError): self.service.purchase_status(self.identity, "a" * 64)
        self.reader.expiry = NOW + 100
        self.reader.accepted = False
        with self.assertRaises(PermissionError): self.service.purchase_status(self.identity, "a" * 64)

    def test_wrong_identity_or_rpc_disagreement_cannot_report_delivery(self):
        with self.assertRaises(MachineError): self.service.purchase_status(None, "a" * 64)
        with self.assertRaises(MachineError): self.service.purchase_status(self.identity | {"chain_id": 1}, "a" * 64)
        self.reader.hashes[1] = "0x" + "b" * 64
        with self.assertRaisesRegex(MachineError, "DISAGREEMENT"):
            self.service.purchase_status(self.identity, "a" * 64)
