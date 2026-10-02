"""Execute/audit ONE owner-authorized x402 sale on Arbitrum Sepolia.

Remote-only operator CLI. Disposable buyer signing stays outside the Economic
Machine service. Secrets never enter artifacts or stdout. Repeated execute is
refused after a transmission attempt; reconcile only reads the existing order.
"""

import argparse
import copy
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data
from sepolia_merchant import ABI, CHAIN_ID, MAX_AMOUNT, RPC, USDC, header, hexbytes
from web3 import Web3

from economic_machine.values import digest
from machine_commerce.domain import money_atoms, now_seconds
from machine_commerce.operations import Operations, Settings, password_hash
from machine_commerce.store import Store

ROOT = Path("/var/lib/machine-commerce-sepolia")
STATE = ROOT / "demo-state.json"
RUNTIME = Path("/var/lib/machine-commerce-sepolia-runtime")
URL = "https://machine.148-113-153-116.nip.io/commerce-sepolia/data"
API = "http://127.0.0.1:4261"
OUTPUT = Path(__file__).resolve().parents[1] / "artifacts/arbitrum-sepolia"


def save(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def chain(rpc=RPC):
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
    if w3.eth.chain_id != CHAIN_ID:
        raise RuntimeError("fixed Arbitrum Sepolia chain guard rejected RPC")
    return w3, w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)


def balance(w3, token, wallets, block="latest"):
    return {role: {"address": wallet["address"],
                   "usdc_atoms": token.functions.balanceOf(wallet["address"]).call(block_identifier=block),
                   "eth_wei": w3.eth.get_balance(wallet["address"], block_identifier=block)}
            for role, wallet in wallets.items()}


def init():
    if STATE.exists():
        raise RuntimeError("demo already initialized; reuse existing protected configuration")
    wallets = json.loads((ROOT / "test-wallets.json").read_text())
    passwords = {role: secrets.token_urlsafe(32) for role in wallets}
    operators = {role: password_hash(password) for role, password in passwords.items()}
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    store = Store(RUNTIME / "runtime.sqlite3", now_seconds)
    bootstrap = Operations(store, Settings(operators=operators))
    identities = {role: bootstrap.login({"username": role, "password": passwords[role]})[0] for role in wallets}
    w3, token = chain()
    block = w3.eth.get_block("finalized")
    data = {"schema": "arbitrum-finalized-block-snapshot-1", "chain_id": CHAIN_ID,
            "block_number": block.number, "block_hash": hexbytes(block.hash), "timestamp": block.timestamp,
            "gas_used": block.gasUsed, "transactions": len(block.transactions),
            "observed_at": int(time.time()), "source": RPC, "assurance": "APPROVED_RPC_FINALIZED_TAG"}
    profile = {"url": URL, "rpc_url": RPC, "network": "eip155:421614", "asset": USDC,
               "pay_to": wallets["seller"]["address"], "token_name": "USD Coin", "token_version": "2",
               "max_timeout_seconds": 120, "seller_owner": identities["seller"],
               "data_type": "arbitrum.finalized-block", "data_version": "block-" + str(block.number),
               "finality": "finalized"}
    save(ROOT / "operators.json", operators)
    save(ROOT / "resources.json", {"sepolia-block": profile})
    save(ROOT / "seller-wallet.json", wallets["seller"])
    state = {"phase": "INITIALIZED", "passwords": passwords, "identities": identities, "profile": profile,
             "data": data, "wallets": {r: {"address": v["address"]} for r, v in wallets.items()}}
    save(STATE, state)
    save(OUTPUT / "preflight.json", {"network": profile["network"], "asset": USDC,
        "token_domain": {"name": "USD Coin", "version": "2", "separator": hexbytes(token.functions.DOMAIN_SEPARATOR().call()),
                         "decimals": token.functions.decimals().call()},
        "wallets": balance(w3, token, wallets), "data_hash": digest(data), "profile": profile}, 0o644)
    print(json.dumps({"phase": state["phase"], "wallets": state["wallets"], "network": profile["network"]}))


@contextmanager
def owner(role, state):
    with httpx.Client(base_url=API, headers={"X-Forwarded-Proto": "https"}, timeout=45, trust_env=False) as client:
        response = client.post("/api/sessions", json={"username": role, "password": state["passwords"][role]})
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.cookies["machine_buyer"]
        yield client


def post(client, path, body):
    response = client.post(path, json=body)
    if response.status_code != 200:
        raise RuntimeError("API " + path + " failed with " + str(response.status_code))
    return response.json()


def agent(secret):
    return httpx.Client(base_url=API, headers={"Authorization": "Bearer " + secret, "X-Forwarded-Proto": "https"},
                        timeout=45, trust_env=False)


def approved_match(matches, supply_id):
    eligible = [match for match in matches if match["terms"]["supply_id"] == supply_id]
    if len(eligible) != 1:
        raise RuntimeError("expected exactly one agreement for the newly approved seller offer")
    return eligible[0]


def prepare():
    state = json.loads(STATE.read_text())
    if state["phase"] != "INITIALIZED":
        raise RuntimeError("reuse the existing prepared payment; no duplicate agreement")
    w3, token = chain()
    balances = balance(w3, token, state["wallets"])
    if balances["buyer"]["usdc_atoms"] < MAX_AMOUNT or balances["seller"]["eth_wei"] < 10**13:
        raise RuntimeError("test USDC or test gas is not funded; no payment prepared")
    profile = state["profile"]
    asset = profile["network"] + "/erc20:" + USDC
    with owner("seller", state) as seller, owner("buyer", state) as buyer:
        seller_key = post(seller, "/api/keys", {"name": "Sepolia snapshot seller", "scopes": ["read", "supplies:write"],
                    "policy_id": None, "ttl_seconds": 86400})
        mandate = post(buyer, "/api/payment-mandates", {"payer": state["wallets"]["buyer"]["address"],
            "payment_asset": asset, "budget": "0.01", "max_order": "0.01", "resources": ["sepolia-block"],
            "ttl_seconds": 86400})
        buyer_key = post(buyer, "/api/keys", {"name": "Sepolia data buyer", "scopes": ["read", "demands:write", "payments:request"],
            "policy_id": None, "payment_mandate_id": mandate["id"], "ttl_seconds": 86400})
    with agent(seller_key["secret"]) as seller, agent(buyer_key["secret"]) as buyer:
        supply = post(seller, "/api/supplies", {"name": "Sepolia Chain Observer", "data_type": profile["data_type"],
            "version": profile["data_version"], "unit_price": "0.0125", "floor_price": "0.01", "discount_bps": 2000,
            "discount_min_units": 1, "min_units": 1, "max_units": 1, "purposes": ["research"], "licenses": ["internal-use"],
            "updated_at": state["data"]["observed_at"], "refresh_seconds": 86400, "response_seconds": 10,
            "ttl_seconds": 86400, "payment_asset": asset})
        demand = post(buyer, "/api/demands", {"data_type": profile["data_type"], "purpose": "research", "license": "internal-use",
            "units": 1, "max_unit_price": "0.01", "max_total_price": "0.01", "max_age_seconds": 86400,
            "max_refresh_seconds": 86400, "response_seconds": 30, "ttl_seconds": 86400, "payment_asset": asset})
        match = approved_match(demand["matches"], supply["id"])
        if money_atoms(match["terms"]["total_price"]) != MAX_AMOUNT:
            raise RuntimeError("negotiated sale exceeds approved test amount")
        payment = post(buyer, "/api/payments", {"match_id": match["id"], "terms_hash": match["terms_hash"],
            "mandate_id": mandate["id"], "resource_id": "sepolia-block",
            "idempotency_key": "public-sepolia-sale-" + str(state.get("unsigned_generation", 1))})
        forbidden = buyer.post("/api/payment-mandates", json={})
        if forbidden.status_code != 403:
            raise RuntimeError("buyer agent unexpectedly has capital administration authority")
    request = {"terms_hash": match["terms_hash"], "data_version": profile["data_version"],
               "units": 1, "purpose": "research", "license": "internal-use"}
    sale = {"profile": profile, "payment_id": payment["id"], "payer": state["wallets"]["buyer"]["address"],
            "amount_atoms": MAX_AMOUNT, "request": request, "data": state["data"]}
    save(ROOT / "sale.json", sale)
    state.update(phase="PREPARED", payment_id=payment["id"], mandate_id=mandate["id"],
                 buyer_secret=buyer_key["secret"], seller_secret=seller_key["secret"], match=match)
    save(STATE, state)
    save(OUTPUT / "agreement.json", {"supply_id": supply["id"], "demand_id": demand["id"], "agreement": match,
        "payment": payment, "mandate": mandate, "buyer_admin_rejected": True,
        "seller_key": seller_key["key"], "buyer_key": buyer_key["key"]}, 0o644)
    print(json.dumps({"phase": state["phase"], "payment_id": payment["id"], "amount_test_usdc": "0.01"}))


def renew():
    """Only an unsent expired preparation may be replaced, never an unknown send."""
    state = json.loads(STATE.read_text())
    if state["phase"] != "PREPARED":
        raise RuntimeError("a signature may have been transmitted; reconcile only")
    with agent(state["buyer_secret"]) as buyer:
        current = buyer.get(f'/api/payments/{state["payment_id"]}').json()
        if current["status"] not in {"PREPARED", "CHALLENGE_READY", "CANCELLED"} or current["tx_hash"]:
            raise RuntimeError("runtime cannot confirm that the old payment was never sent")
        cancelled = post(buyer, f'/api/payments/{state["payment_id"]}/cancel', {})
        if cancelled["status"] != "CANCELLED":
            raise RuntimeError("unsigned hold was not released")
    history = state.get("unsigned_preparations", [])
    history.append({"payment_id": state["payment_id"], "status": "CANCELLED", "signature_transmitted": False})
    state.update(phase="INITIALIZED", unsigned_preparations=history,
                 unsigned_generation=state.get("unsigned_generation", 1) + 1)
    save(STATE, state)
    prepare()


def execute():
    state = json.loads(STATE.read_text())
    if state["phase"] != "PREPARED":
        raise RuntimeError("a previous send may have occurred; run reconcile, never execute again")
    w3, token = chain()
    before_block = w3.eth.block_number
    state["before"] = balance(w3, token, state["wallets"], before_block)
    state["before_block"] = before_block
    with agent(state["buyer_secret"]) as buyer:
        ready = post(buyer, f'/api/payments/{state["payment_id"]}/challenge', {})
        typed = ready["typed_data"]
        if (typed["domain"] != {"name": "USD Coin", "version": "2", "chainId": CHAIN_ID, "verifyingContract": USDC}
                or typed["message"]["value"] != MAX_AMOUNT
                or typed["message"]["from"].lower() != state["wallets"]["buyer"]["address"].lower()
                or typed["message"]["to"].lower() != state["wallets"]["seller"]["address"].lower()):
            raise RuntimeError("external buyer signing guard rejected changed economic authority")
        keys = json.loads((ROOT / "test-wallets.json").read_text())
        signed = Account.sign_message(encode_typed_data(full_message=typed), private_key=keys["buyer"]["private_key"])
        template = copy.deepcopy(ready["payment_template"])
        template["payload"]["signature"] = hexbytes(signed.signature)
        state.update(phase="SUBMISSION_ATTEMPTED", authorization=ready["payment_template"]["payload"]["authorization"],
                     typed_data_hash=digest(typed), signed_payload_hash=digest(template))
        save(STATE, state)  # Commit before HTTP. No plaintext signature in the file.
        try:
            result = post(buyer, f'/api/payments/{state["payment_id"]}/submit', {"payment_signature": header(template)})
        except (httpx.HTTPError, RuntimeError, ValueError):
            print(json.dumps({"status": "UNKNOWN", "payment_id": state["payment_id"], "safe_to_retry_payment": False}))
            return
        state["last_result"] = result
        save(STATE, state)
        save(OUTPUT / "submission.json", {"payment": result, "before": state["before"], "before_block": before_block,
             "typed_data_hash": state["typed_data_hash"], "signed_payload_hash": state["signed_payload_hash"],
             "authorization": state["authorization"], "external_test_signer": True}, 0o644)
        print(json.dumps({"status": result["status"], "tx_hash": result["tx_hash"], "delivery_received": bool(result["delivery"])}))


def reconcile():
    state = json.loads(STATE.read_text())
    if state["phase"] not in {"SUBMISSION_ATTEMPTED", "VERIFIED"}:
        raise RuntimeError("no transmitted payment to reconcile")
    with agent(state["buyer_secret"]) as buyer:
        result = post(buyer, f'/api/payments/{state["payment_id"]}/reconcile', {})
        snapshot = buyer.get("/api/payments").json()
        # Submitting a terminal/pending record again must not contact the merchant.
        replay = post(buyer, f'/api/payments/{state["payment_id"]}/submit', {"payment_signature": "intentionally-invalid-replay"})
        if replay["id"] != result["id"] or replay["tx_hash"] != result["tx_hash"]:
            raise RuntimeError("at-most-once replay changed the persisted payment")
    state["last_result"] = result
    save(STATE, state)
    save(OUTPUT / "reconciliation.json", {"payment": result, "snapshot": snapshot,
        "replay_kept_same_payment": True, "checked_at": int(time.time())}, 0o644)
    print(json.dumps({"status": result["status"], "tx_hash": result["tx_hash"], "observation": result["observation"]}))
    if result["status"] == "SETTLED":
        audit(state, result, snapshot)


def audit(state, result, snapshot):
    w3, token = chain()
    tx_hash = result["tx_hash"]
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    finalized = w3.eth.get_block("finalized")
    if receipt.status != 1 or receipt.blockNumber > finalized.number:
        raise RuntimeError("receipt is not successful and finalized")
    block = w3.eth.get_block(receipt.blockNumber)
    if block.hash != receipt.blockHash:
        raise RuntimeError("noncanonical receipt")
    after = balance(w3, token, state["wallets"], receipt.blockNumber)
    deltas = {role: {field: after[role][field] - state["before"][role][field] for field in ["usdc_atoms", "eth_wei"]}
              for role in ["buyer", "seller"]}
    if (deltas["buyer"]["usdc_atoms"] != -MAX_AMOUNT or deltas["seller"]["usdc_atoms"] != MAX_AMOUNT
            or deltas["buyer"]["eth_wei"] != 0 or deltas["seller"]["eth_wei"] >= 0):
        raise RuntimeError("independent payer/recipient/gas balance reconciliation failed")
    if not token.functions.authorizationState(state["wallets"]["buyer"]["address"],
            bytes.fromhex(state["authorization"]["nonce"][2:])).call(block_identifier=finalized.number):
        raise RuntimeError("authorization nonce is not consumed at finalized state")
    artifact = result["delivery"]["artifact"]
    if (artifact["terms_hash"] != state["match"]["terms_hash"] or artifact["data_version"] != state["profile"]["data_version"]
            or digest(artifact["data"]) != digest(state["data"]) or digest(artifact) != result["delivery"]["artifact_hash"]):
        raise RuntimeError("data delivery terms, version or content hash mismatch")
    source = w3.eth.get_block(artifact["data"]["block_number"])
    if hexbytes(source.hash) != artifact["data"]["block_hash"]:
        raise RuntimeError("delivered block snapshot differs from public chain")
    mandate = next(m for m in snapshot["mandates"] if m["id"] == state["mandate_id"])
    if mandate["reserved"] != 0 or mandate["spent"] != MAX_AMOUNT:
        raise RuntimeError("capital hold/spend does not match settled payment")
    # Independent provider must also see the canonical receipt and exact balances.
    independent, independent_token = chain("https://arbitrum-sepolia.drpc.org")
    independent_receipt = independent.eth.get_transaction_receipt(tx_hash)
    if (independent_receipt.status != 1 or independent_receipt.blockHash != receipt.blockHash
            or balance(independent, independent_token, state["wallets"], receipt.blockNumber) != after):
        raise RuntimeError("independent RPC disagrees with settlement evidence")
    merchant_path = Path("/var/lib/machine-commerce-sepolia-merchant/journal.sqlite3")
    import sqlite3
    with sqlite3.connect(f"file:{merchant_path}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id,tx_hash,status,broadcasts FROM sales").fetchall()
    if rows != [(state["payment_id"], tx_hash, "RECEIPT_CONFIRMED", 1)]:
        raise RuntimeError("seller journal does not prove one approved broadcast")
    save(OUTPUT / "receipt.json", json.loads(Web3.to_json(receipt)), 0o644)
    proof = {"schema": "public-arbitrum-sepolia-x402-proof-1", "verified": True, "mainnet": False,
        "chain_id": CHAIN_ID, "token": USDC, "test_usdc": "0.01", "tx_hash": tx_hash,
        "explorer": "https://sepolia.arbiscan.io/tx/" + tx_hash,
        "payment_id": state["payment_id"], "terms_hash": state["match"]["terms_hash"],
        "receipt_block": receipt.blockNumber, "receipt_block_hash": hexbytes(receipt.blockHash),
        "rpc_finalized_block": finalized.number, "finality_assurance": "RPC_FINALIZED_TAG_NOT_INDEPENDENT_L1_PROOF",
        "independent_rpc": "https://arbitrum-sepolia.drpc.org", "before_block": state["before_block"],
        "before": state["before"], "after_at_receipt_block": after, "balance_deltas": deltas,
        "authorization_nonce": state["authorization"]["nonce"], "nonce_consumed": True,
        "artifact": artifact, "artifact_hash": digest(artifact), "data_truth_scope": "PUBLIC_BLOCK_HASH_READBACK",
        "mandate": mandate, "seller_broadcasts": 1, "replayed_submit_did_not_pay_again": True,
        "buyer_runtime_custody": "NONE", "customer_wallet_used": False, "language_model_calls": 0,
        "unsigned_preparations": state.get("unsigned_preparations", []),
        "checked_at": int(time.time())}
    save(OUTPUT / "proof.json", proof, 0o644)
    state["phase"] = "VERIFIED"
    save(STATE, state)
    print(json.dumps({"verified": True, "tx_hash": tx_hash, "test_usdc": "0.01", "payer_delta": -MAX_AMOUNT,
                      "recipient_delta": MAX_AMOUNT, "seller_broadcasts": 1}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "prepare", "renew", "execute", "reconcile"])
    args = parser.parse_args()
    globals()[args.command]()


if __name__ == "__main__":
    main()
