"""Remote-only public quote evidence. Never submits an order or transaction.

The optional owner is a public address. A fresh unfunded throwaway identity signs
only the simulation permit; its key and signatures never leave this process's
memory except for the permit supplied to the quote simulator, and are not logged.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_typed_data

from machine_commerce.gas_router import API, RELAYER, GasRouter, read_json, typed

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Run on the authorized remote host.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True, help="Public wallet address, never a key")
    args = parser.parse_args()
    calls = []

    def quote_only(method, url, body=None):
        if method != "POST" or url != API + "/quote":
            raise RuntimeError("ORDER_SUBMISSION_FORBIDDEN_IN_PROBE")
        calls.append("POST /quote")
        return read_json(method, url, body)

    router = GasRouter(api=quote_only)
    owner = router.prepare(args.owner, 2000000)
    account = Account.create()
    rows = router.balances(account.address)
    if any(int(r["usdc_atoms"]) or int(r["eth_wei"]) for r in rows):
        raise RuntimeError("UNFUNDED_TEST_IDENTITY_REQUIRED")
    deadline = int(time.time()) + 600
    permit = typed("Permit", {"owner": account.address, "spender": RELAYER, "value": "2000000",
                               "nonce": rows[0]["nonce"], "deadline": deadline})
    signed = Account.sign_message(encode_typed_data(full_message=permit), account.key)
    result = router.order({"owner": account.address, "amount_atoms": "2000000", "valid_to": deadline,
                           "permit": permit, "observations": rows}, "0x" + signed.signature.hex())
    evidence = {"observed_at": int(time.time()), "kind": "LIVE_MAINNET_READ_AND_QUOTE_SIMULATION_ONLY",
        "owner_address_redacted": True,
        "owner_observations": [{"rpc": row["rpc"], "enough_usdc": int(row["usdc_atoms"]) >= 2000000,
                                "eth_wei": row["eth_wei"], "nonce": row["nonce"]} for row in owner["observations"]],
        "owner_quote_minimum_wei": owner["preview_minimum_wei"], "price_guard": owner["price_guard"],
        "test_identity": {"unfunded": True, "fresh_ephemeral": True, "verified_quote": result["verified_quote"],
                          "minimum_buy_wei": result["minimum_buy_wei"], "fee_atoms": result["estimated_fee_atoms"]},
        "cow_api_calls": calls, "orders_submitted": 0, "transactions_broadcast": 0, "owner_signatures": 0,
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [ROOT/"src/machine_commerce/gas_router.py", ROOT/"src/machine_commerce/fuel_price.py", Path(__file__)]}}
    output = ROOT/"artifacts/fuel/live-readonly-probe.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2)+"\n")
    print(json.dumps({"evidence": str(output), "verified_quote": result["verified_quote"],
                      "orders_submitted": 0, "transactions_broadcast": 0}))


if __name__ == "__main__":
    main()
