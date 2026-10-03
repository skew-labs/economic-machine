"""One bounded, journaled deployment using the existing disposable test seller.

The runtime never acquires this signer. There is no mainnet option, transfer,
release registration or purchase in this command. A transmitted deployment can
only be reconciled, never replaced automatically.
"""

import argparse
import fcntl
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from eth_account import Account
from eth_utils import keccak
from web3 import Web3

from economic_machine.values import MachineError, digest
from machine_commerce.datapass import deployment_draft

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path("/var/lib/machine-commerce-sepolia/datapass-deployment.json")
KEY = Path("/var/lib/machine-commerce-sepolia/seller-wallet.json")
PUBLIC = ROOT / "artifacts/arbitrum-sepolia/datapass-deployment.json"
CHAIN = 421614
OWNER = "0xd26491D35Ed8725Ef2bd1E3DB1A1F8959ed31e97"
TOKEN = "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d"
RPCS = ("https://sepolia-rollup.arbitrum.io/rpc", "https://arbitrum-sepolia.drpc.org")
MAX_GAS = 1_400_000_000_000_000


def hx(value):
    return "0x" + bytes(value).hex()


def save(path, value, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    os.fchmod(fd, mode)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def connect(url):
    client = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
    if client.eth.chain_id != CHAIN:
        raise MachineError("DEPLOYMENT_CHAIN_MISMATCH")
    return client


def compiled():
    report = json.loads((ROOT / "artifacts/atlas-release/datapass-build.json").read_text())
    source = ROOT / "contracts/SkewDataPass.sol"
    if hashlib.sha256(source.read_bytes()).hexdigest() != report["sources"][source.name]:
        raise MachineError("SOURCE_BUILD_MISMATCH")
    value = json.loads((ROOT / "artifacts/contracts.json").read_text())["SkewDataPass"]
    for field, report_field in (("bytecode", "bytecode_sha256"), ("runtime", "runtime_sha256")):
        if hashlib.sha256(bytes.fromhex(value[field])).hexdigest() != report["contracts"]["SkewDataPass"][report_field]:
            raise MachineError("BYTECODE_BUILD_MISMATCH")
    return value, report


def prepare():
    if PRIVATE.exists():
        state = json.loads(PRIVATE.read_text())
        if state["phase"] != "PREPARED":
            raise MachineError("DEPLOYMENT_ALREADY_ATTEMPTED_RECONCILE_ONLY")
        print(json.dumps(state["review"]))
        return
    value, report = compiled()
    clients = [connect(url) for url in RPCS]
    w3 = clients[0]
    finalized = min(c.eth.get_block("finalized").number for c in clients)
    hashes = [hx(c.eth.get_block(finalized).hash) for c in clients]
    if len(set(hashes)) != 1:
        raise MachineError("FINALIZED_BLOCK_DISAGREEMENT")
    token_code = [hx(c.eth.get_code(TOKEN, finalized)) for c in clients]
    if len(set(token_code)) != 1 or token_code[0] == "0x":
        raise MachineError("TEST_USDC_CODE_DISAGREEMENT")
    nonce = w3.eth.get_transaction_count(OWNER, "pending")
    if nonce != w3.eth.get_transaction_count(OWNER, "latest"):
        raise MachineError("PENDING_SELLER_TRANSACTION")
    draft = deployment_draft(OWNER)
    estimate = w3.eth.estimate_gas({"from": OWNER, "data": draft["data"], "value": 0})
    gas = (estimate * 12 + 9) // 10
    price = w3.eth.gas_price * 2
    if gas * price > MAX_GAS or w3.eth.get_balance(OWNER) < gas * price:
        raise MachineError("TEST_GAS_CAP_OR_BALANCE_INSUFFICIENT")
    tx = {"chainId": CHAIN, "nonce": nonce, "value": 0, "gas": gas,
          "gasPrice": price, "data": draft["data"]}
    review = {"schema": "datapass-public-deployment-review-1", "chain_id": CHAIN,
              "owner": OWNER, "payment_asset": TOKEN, "contract": "SkewDataPass",
              "source_sha256": report["sources"]["SkewDataPass.sol"],
              "runtime_sha256": hashlib.sha256(bytes.fromhex(value["runtime"])).hexdigest(),
              "nonce": nonce, "gas": gas, "gas_price_wei": price,
              "maximum_test_gas_wei": gas * price, "value_wei": 0,
              "token_transfer": False, "finalized_block": finalized,
              "finalized_block_hash": hashes[0], "created_at": int(time.time()),
              "authority": "OWNER_REQUEST_PUBLIC_TESTNET_DEPLOYMENT_EXISTING_DISPOSABLE_SELLER"}
    review["review_sha256"] = digest(review)
    state = {"phase": "PREPARED", "review": review, "transaction": tx}
    save(PRIVATE, state, 0o600)
    save(PUBLIC.with_name("datapass-deployment-review.json"), review, 0o644)
    print(json.dumps(review))


def execute(review_hash):
    state = json.loads(PRIVATE.read_text())
    if state["phase"] != "PREPARED" or state["review"]["review_sha256"] != review_hash:
        raise MachineError("EXACT_PREPARED_REVIEW_REQUIRED")
    if time.time() - state["review"]["created_at"] > 600:
        raise MachineError("DEPLOYMENT_REVIEW_EXPIRED")
    compiled()
    w3 = connect(RPCS[0])
    tx = state["transaction"]
    if (tx["chainId"] != CHAIN or tx["value"] != 0 or tx["gas"] * tx["gasPrice"] > MAX_GAS
            or tx["data"] != deployment_draft(OWNER)["data"]
            or w3.eth.get_transaction_count(OWNER, "pending") != tx["nonce"]
            or w3.eth.get_transaction_count(OWNER, "latest") != tx["nonce"]
            or w3.eth.gas_price > tx["gasPrice"] or w3.eth.get_balance(OWNER) < tx["gas"] * tx["gasPrice"]):
        raise MachineError("DEPLOYMENT_REVALIDATION_FAILED")
    metadata = KEY.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise MachineError("PRIVATE_TEST_SIGNER_FILE_REQUIRED")
    material = json.loads(KEY.read_text())
    account = Account.from_key(material["private_key"])
    if account.address != OWNER or material["address"].lower() != OWNER.lower():
        raise MachineError("DISPOSABLE_SELLER_MISMATCH")
    signed = account.sign_transaction(tx)
    # Preserve the exact transaction hash before any network transmission.
    # A timeout retains this record and prohibits a replacement deployment.
    state.update(phase="TRANSMISSION_ATTEMPTED", tx_hash=hx(signed.hash), attempted_at=int(time.time()))
    save(PRIVATE, state, 0o600)
    try:
        observed = hx(w3.eth.send_raw_transaction(signed.raw_transaction))
        if observed != state["tx_hash"]:
            raise MachineError("BROADCAST_HASH_MISMATCH")
        state["phase"] = "SUBMITTED"
        save(PRIVATE, state, 0o600)
    except Exception:  # noqa: BLE001 - no blind retry and no provider/credential exception text.
        print(json.dumps({"status": "UNKNOWN_RECONCILE_ONLY", "tx_hash": state["tx_hash"]}))
        return
    print(json.dumps({"status": "SUBMITTED", "tx_hash": state["tx_hash"]}))


def reconcile():
    state = json.loads(PRIVATE.read_text())
    if state["phase"] == "PREPARED":
        raise MachineError("NO_DEPLOYMENT_TRANSMITTED")
    value, build = compiled()
    observations = []
    for url in RPCS:
        client = connect(url)
        receipt = client.eth.get_transaction_receipt(state["tx_hash"])
        transaction = client.eth.get_transaction(state["tx_hash"])
        final = client.eth.get_block("finalized")
        if receipt.status != 1 or not receipt.contractAddress:
            raise MachineError("DEPLOYMENT_TRANSACTION_FAILED")
        block = client.eth.get_block(receipt.blockNumber)
        if (transaction["from"] != OWNER or transaction["to"] is not None or transaction["value"] != 0
                or hx(transaction["input"]) != state["transaction"]["data"]
                or transaction["nonce"] != state["transaction"]["nonce"]
                or hx(receipt.blockHash) != hx(block.hash)):
            raise MachineError("DEPLOYMENT_TRANSACTION_MISMATCH")
        if final.number < receipt.blockNumber:
            print(json.dumps({"status": "INCLUDED_AWAITING_RPC_FINALIZED_TAG", "tx_hash": state["tx_hash"],
                              "block": receipt.blockNumber, "finalized_block": final.number}))
            return
        code = client.eth.get_code(receipt.contractAddress, receipt.blockNumber)
        expected = bytes.fromhex(value["runtime"])
        if code != expected:
            raise MachineError("DEPLOYED_RUNTIME_MISMATCH")
        contract = client.eth.contract(address=receipt.contractAddress, abi=value["abi"])
        owner = contract.functions.owner().call(block_identifier=receipt.blockNumber)
        publisher = contract.functions.publishers(OWNER).call(block_identifier=receipt.blockNumber)
        asset = contract.functions.paymentAssets(TOKEN).call(block_identifier=receipt.blockNumber)
        paused = contract.functions.paused().call(block_identifier=receipt.blockNumber)
        supply = contract.functions.nextTokenId().call(block_identifier=receipt.blockNumber)
        if owner != OWNER or not publisher or not asset or paused or supply != 1:
            raise MachineError("DEPLOYED_CONSTRUCTOR_STATE_MISMATCH")
        observations.append({"rpc": url, "chain_id": CHAIN, "tx_hash": state["tx_hash"],
            "contract_address": receipt.contractAddress, "block_number": receipt.blockNumber,
            "block_hash": hx(receipt.blockHash), "finalized_block": final.number,
            "runtime_sha256": hashlib.sha256(code).hexdigest(), "runtime_keccak256": hx(keccak(code)),
            "owner": owner, "publisher_authorized": publisher, "test_usdc_allowed": asset,
            "paused": paused, "next_token_id": supply, "gas_used": receipt.gasUsed,
            "gas_price_wei": receipt.effectiveGasPrice,
            "actual_test_gas_wei": receipt.gasUsed * receipt.effectiveGasPrice,
            "receipt_status": receipt.status})
    first, second = observations
    for field in ("contract_address", "block_hash", "runtime_sha256", "owner", "gas_used"):
        if first[field] != second[field]:
            raise MachineError("INDEPENDENT_DEPLOYMENT_READBACK_DISAGREEMENT")
    proof = {"schema": "datapass-public-deployment-proof-1", "status": "PUBLIC_TESTNET_DEPLOYED",
             "network": "eip155:421614", "contract_address": first["contract_address"],
             "tx_hash": state["tx_hash"], "source_sha256": build["sources"]["SkewDataPass.sol"],
             "review_sha256": state["review"]["review_sha256"], "observations": observations,
             "checked_at": int(time.time()), "scope": "RPC_FINALIZED_TAG_NOT_MAINNET_OR_SECURITY_AUDIT",
             "purchase_executed": False, "customer_funds_used": False,
             "explorer": "https://sepolia.arbiscan.io/address/" + first["contract_address"]}
    proof["proof_sha256"] = digest(proof)
    save(PUBLIC, proof, 0o644)
    state["phase"] = "VERIFIED"
    save(PRIVATE, state, 0o600)
    print(json.dumps(proof))


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Deployment control runs only on the authorized remote host")
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "execute", "reconcile"))
    parser.add_argument("--review-sha256")
    args = parser.parse_args()
    # The merchant shares this account. This operator process serializes itself;
    # on-chain pending/latest nonce checks reject other account activity.
    fd = os.open(PRIVATE.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        {"prepare": prepare, "execute": lambda: execute(args.review_sha256), "reconcile": reconcile}[args.operation]()
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
