"""Read-only join of the completed runtime purchase, chain signature and delivery.

Run only on Canada. Never reads wallet key files, signs, broadcasts or mutates DB.
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from audit_sepolia_proof import RPCS, audit_rpc, check_record
from eth_account import Account
from eth_account.messages import encode_typed_data
from sepolia_merchant import ABI
from web3 import Web3

from economic_machine.journal import verify_journal
from economic_machine.values import digest
from machine_commerce.evidence import build_completed_trade
from machine_commerce.payments import typed_authorization
from machine_commerce.x402 import PaymentBinding


def read_record(db_path, proof):
    with sqlite3.connect("file:" + str(db_path.resolve()) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")  # One consistent read snapshot including the WAL.
        def row(table, rid):
            allowed = {"payments", "matches", "demands", "supplies", "payment_mandates"}
            if table not in allowed:
                raise ValueError("unknown evidence table")
            found = db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
            if found is None:
                raise ValueError("completed trade row unavailable")
            result = dict(found)
            for key in ["body", "delivery", "observation"]:
                if key in result and result[key] is not None:
                    result[key] = json.loads(result[key])
            return result
        payment = row("payments", proof["payment_id"])
        match = row("matches", payment["match_id"])
        events = [{"ordinal": e["ordinal"], "kind": e["kind"], "event": json.loads(e["event_json"]),
                   "event_hash": e["event_hash"], "previous_hash": e["previous_hash"]}
                  for e in db.execute("SELECT * FROM events WHERE event_id LIKE ? ORDER BY ordinal", (payment["id"] + ":%",))]
        return {"proof": proof, "payment": payment, "match": match,
                "demand": row("demands", match["demand_id"]), "supply": row("supplies", match["supply_id"]),
                "mandate": row("payment_mandates", payment["mandate_id"]), "events": events,
                "journal_verified": verify_journal(db)}


def signature_check(record, rpc):
    payment, proof = record["payment"], record["proof"]
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
    if w3.eth.chain_id != 421614:
        raise ValueError("wrong signature observation chain")
    tx = w3.eth.get_transaction(proof["tx_hash"])
    if tx["to"].lower() != proof["token"].lower() or tx["value"] != 0:
        raise ValueError("transaction is not the exact token authorization")
    token = w3.eth.contract(address=Web3.to_checksum_address(proof["token"]), abi=ABI)
    function, args = token.decode_function_input(tx["input"])
    if function.fn_name != "transferWithAuthorization":
        raise ValueError("token call is not EIP-3009")
    auth = {"from": args["from"].lower(), "to": args["to"].lower(), "value": str(args["value"]),
            "validAfter": str(args["validAfter"]), "validBefore": str(args["validBefore"]),
            "nonce": "0x" + bytes(args["nonce"]).hex()}
    if auth != payment["body"]["authorization"]:
        raise ValueError("chain authorization differs from prepared runtime state")
    signature = "0x" + (bytes(args["r"]) + bytes(args["s"]) + bytes([args["v"]])).hex()
    binding = PaymentBinding(**payment["body"]["binding"])
    signer = Account.recover_message(encode_typed_data(full_message=typed_authorization(binding, auth)), signature=signature)
    payload = {"x402Version": 2, "resource": {"url": binding.resource_url},
               "accepted": payment["body"]["accepted"], "payload": {"authorization": auth, "signature": signature}}
    if signer.lower() != auth["from"] or digest(payload) != payment["signature_hash"]:
        raise ValueError("chain signature is not the authorization recorded by the app")
    block = w3.eth.get_block(tx["blockNumber"])
    if not int(auth["validAfter"]) < block.timestamp < int(auth["validBefore"]):
        raise ValueError("authorization timing disagrees with the execution block")
    return {"rpc": rpc, "recovered_payer": signer, "payload_hash": digest(payload), "authorization_matches": True}


def save_public(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archived-audit", type=Path, required=True)
    parser.add_argument("--fixture-output", type=Path)
    args = parser.parse_args()
    proof = json.loads(args.proof.read_text())
    check_record(proof)
    record = read_record(args.db, proof)
    # Public RPCs can prune historical account state. Never relabel the preserved
    # successful balance audit as a fresh balance observation.
    archived_audit = json.loads(args.archived_audit.read_text())
    observations = [audit_rpc(proof, url, historical_balances=False) for url in RPCS]
    signatures = [signature_check(record, url) for url in RPCS]
    bundle = build_completed_trade(record, observations, signatures, archived_audit)
    save_public(args.output, bundle)
    if args.fixture_output:
        # These selected tables contain no account credentials or wallet key material.
        save_public(args.fixture_output, {"record": record, "observations": observations,
                                         "signature_checks": signatures, "archived_audit": archived_audit})
    print(json.dumps({"verified": True, "read_only": True, "payment_id": bundle["payment"]["id"],
                      "bundle_hash": bundle["bundle_hash"], "signature_matches_runtime": True,
                      "rpc_count": len(observations), "new_signatures": 0, "new_broadcasts": 0}))


if __name__ == "__main__":
    main()
