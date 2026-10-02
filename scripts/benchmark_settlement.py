"""Shared x402 test-token settlement stage, never a public-chain purchase.

Reuse the already-verified merchant/RPC test transport. EIP-712 signing is done
by an ephemeral fixture owner outside Payments; no customer material is loaded.
"""

import copy
import json
import sys
import time
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_tester import EthereumTester, PyEVMBackend
from web3 import EthereumTesterProvider, Web3

from machine_commerce.domain import money_atoms
from machine_commerce.market import Market
from machine_commerce.payments import Payments
from machine_commerce.store import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_payments import EVMSeller, header, hexbytes


class SettlementFixture:
    def __init__(self, directory, wall_clock=None):
        self.directory = directory
        self.wall_clock = wall_clock or time.time
        self.backend = PyEVMBackend()
        self.tester = EthereumTester(self.backend)
        self.w3 = Web3(EthereumTesterProvider(self.tester))
        compiled = json.loads((ROOT / "artifacts/contracts.json").read_text())["TestEIP3009Token"]
        factory = self.w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
        receipt = self.w3.eth.wait_for_transaction_receipt(factory.constructor().transact(
            {"from": self.w3.eth.accounts[0]}))
        self.token = self.w3.eth.contract(address=receipt.contractAddress, abi=compiled["abi"])
        self.token.functions.mint(self.w3.eth.accounts[0], 100_000_000).transact(
            {"from": self.w3.eth.accounts[0]})
        self.asset = f"eip155:{self.w3.eth.chain_id}/erc20:{self.token.address.lower()}"
        self.records = []

    def settle(self, arm, trade, demand, supply):
        # PyEVM's latest block only advances on fixture transactions, whereas
        # gas estimation sees the real clock after a long HTTPS inference wait.
        # Align the ephemeral chain BEFORE creating the short-lived authorization.
        # This has no effect on public RPCs, decisions or measured model latency.
        wall = int(self.wall_clock())
        if wall > self.w3.eth.get_block("latest").timestamp:
            self.tester.time_travel(wall + 1)
            self.tester.mine_block()
        # All compatible fixture offers cost exactly 400000 atoms. Reject any
        # inconsistent adapter amount instead of silently converting TEST_CREDIT.
        clock = lambda: self.w3.eth.get_block("latest").timestamp
        store = Store(self.directory / f"{arm}-{trade}.sqlite3", clock)
        buyer, _ = store.create_session()
        seller, _ = store.create_session()
        market = Market(store)
        market.register(seller, "supply", supply)
        match = market.register(buyer, "demand", demand)["matches"][0]
        if match["terms"]["asset"] != self.asset or money_atoms(match["terms"]["total_price"]) != 400_000:
            raise ValueError("benchmark asset or amount does not match the exact token challenge")
        profile = {"url": "https://seller.example/data", "rpc_url": "https://rpc.example/chain",
            "network": f"eip155:{self.w3.eth.chain_id}", "asset": self.token.address,
            "pay_to": self.w3.eth.accounts[1], "token_name": "Machine Payment Test", "token_version": "1",
            "max_timeout_seconds": 60, "seller_owner": seller, "data_type": supply["data_type"],
            "data_version": supply["version"], "finality": "finalized"}
        transport = EVMSeller(self.w3, self.token, profile)
        payments = Payments(store, market, {"batch-data": profile}, transport)
        mandate = payments.mandate(buyer, {"payer": self.w3.eth.accounts[0], "payment_asset": self.asset,
            "budget": "0.5", "max_order": "0.5", "resources": ["batch-data"], "ttl_seconds": 3600})
        ready = payments.challenge(buyer, payments.prepare(buyer, {"match_id": match["id"],
            "terms_hash": match["terms_hash"], "mandate_id": mandate["id"], "resource_id": "batch-data",
            "idempotency_key": f"benchmark-{arm}-{trade}"})["id"])
        payload = copy.deepcopy(ready["payment_template"])
        signed = Account.sign_message(encode_typed_data(full_message=ready["typed_data"]),
                                      private_key=self.backend.account_keys[0])
        payload["payload"]["signature"] = hexbytes(signed.signature)
        before = self.token.functions.balanceOf(self.w3.eth.accounts[1]).call()
        done = payments.submit(buyer, ready["id"], header(payload))
        after = self.token.functions.balanceOf(self.w3.eth.accounts[1]).call()
        mandate = payments.snapshot(buyer)["mandates"][0]
        verified = (done["status"] == "SETTLED" and done["observation"]["status"] == "PAID"
                    and after - before == 400_000 and mandate["spent"] == 400_000
                    and mandate["reserved"] == 0 and transport.signed_calls == 1)
        record = {"arm": arm, "trade": trade, "verified": verified, "environment": "EPHEMERAL_PYEVM",
            "public_chain_payment": False, "payment": done, "recipient_delta_atoms": after - before,
            "signed_submissions": transport.signed_calls, "spent_atoms": mandate["spent"],
            "reserved_atoms": mandate["reserved"]}
        self.records.append(record)
        if not verified:
            (self.directory / "failed-settlement.json").write_text(json.dumps(record, indent=2) + "\n")
            raise RuntimeError("test-token settlement or delivery verification failed")
        return record

    def balances(self):
        return {"payer_atoms": self.token.functions.balanceOf(self.w3.eth.accounts[0]).call(),
                "recipient_atoms": self.token.functions.balanceOf(self.w3.eth.accounts[1]).call(),
                "initial_payer_atoms": 100_000_000}
