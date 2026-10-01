"""Read-only production admission checks; never sign, pay or submit transactions."""

import argparse
import json
import os
import sys

from eth_account.messages import hash_domain

from economic_machine.journal import verify_journal
from machine_commerce.operations import Settings
from machine_commerce.payments import validate_profiles
from machine_commerce.store import Store
from machine_commerce.transport import HTTPS, Chain, integer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {"ready": False, "signing_authority": "NONE", "checks": [],
        "assurance": "Read-only configuration, journal and token interface checks. Not a live payment or an audit."}
    try:
        reason = "CONFIGURATION_OR_RESOURCE_CHECK_FAILED"
        settings = Settings.environment()
        if settings.mode != "production":
            reason = "PRODUCTION_MODE_REQUIRED"
            raise ValueError("PRODUCTION_MODE_REQUIRED")
        profiles = validate_profiles(settings.resources)
        if not os.environ.get("COMMERCE_DB") or not os.path.isfile(os.environ["COMMERCE_DB"]):
            reason = "EXISTING_DURABLE_DATABASE_REQUIRED"
            raise ValueError("EXISTING_DURABLE_DATABASE_REQUIRED")
        store = Store(os.environ["COMMERCE_DB"])
        with store.connect() as db:
            if not verify_journal(db):
                reason = "JOURNAL_INTEGRITY_FAILED"
                raise ValueError("JOURNAL_INTEGRITY_FAILED")
            owners = {row[0] for row in db.execute("SELECT s.id,o.subject FROM sessions s JOIN operator_identities o "
                "ON o.session_id=s.id WHERE s.expires>?", (store.clock(),)) if row[1] in settings.operators}
        transport = HTTPS({p["rpc_url"] for p in profiles.values()})
        chain = Chain(transport)
        for rid, p in profiles.items():
            checks = {"resource_id": rid, "registered_seller_active": p["seller_owner"] in owners}
            checks["chain_matches"] = integer(chain.rpc(p["rpc_url"], "eth_chainId", [])) == int(p["network"].split(":")[1])
            checks["finalized_head_available"] = isinstance(chain.rpc(p["rpc_url"], "eth_getBlockByNumber", ["finalized", False]), dict)
            code = chain.rpc(p["rpc_url"], "eth_getCode", [p["asset"], "latest"])
            checks["token_contract_exists"] = isinstance(code, str) and len(code) > 2
            checks["six_decimal_token"] = integer(chain.rpc(p["rpc_url"], "eth_call", [{"to": p["asset"], "data": "0x313ce567"}, "latest"])) == 6
            domain = "0x" + hash_domain({"name": p["token_name"], "version": p["token_version"],
                "chainId": int(p["network"].split(":")[1]), "verifyingContract": p["asset"]}).hex()
            separator = chain.rpc(p["rpc_url"], "eth_call", [{"to": p["asset"], "data": "0x3644e515"}, "latest"])
            checks["eip712_domain_matches"] = isinstance(separator, str) and separator.lower() == domain.lower()
            result["checks"].append(checks)
        result["ready"] = bool(result["checks"]) and all(all(v for k, v in check.items() if k != "resource_id") for check in result["checks"])
    except Exception as exc:  # noqa: BLE001 - configuration URLs may contain credentials; report only the exception type.
        result["error_class"] = type(exc).__name__
        result["reason"] = reason
    if args.output:
        with open(args.output, "w") as output:
            json.dump(result, output, indent=2)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ready"] else 1)


if __name__ == "__main__":
    main()
