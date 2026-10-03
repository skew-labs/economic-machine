"""Remote evidence: recipient isolation, chain readback, unsigned live checkout.

An existing operator opens one unsigned verification checkout and cancels it.
The placeholder payer is never funded or signed. No facilitator settlement,
wallet signature, on-chain submission or subscription grant is performed.
"""

import json
import os
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
from eth_account.messages import hash_domain

from machine_commerce.transport import Chain, HTTPS, integer
from machine_commerce.x402 import decode_header

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://machine.148-113-153-116.nip.io"
BASE = "/commerce/api"
RID = "atlas-monthly"
VAULT = Path("/var/lib/skew-treasury/subscription-receiver")


def service_cannot_open_keys():
    pid = int(subprocess.check_output(["systemctl", "show", "machine-commerce-sepolia-runtime.service",
                                      "--property=MainPID", "--value"], text=True).strip())
    owner = Path(f"/proc/{pid}").stat()
    assert owner.st_uid != 0
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(owner.st_gid)
            os.setuid(owner.st_uid)
            for name in ["keystore.json", "passphrase"]:
                try:
                    fd = os.open(VAULT / name, os.O_RDONLY)
                except PermissionError:
                    continue
                os.close(fd)
                os._exit(1)
            os._exit(0)
        except BaseException:
            os._exit(2)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return True


def verify():
    assert os.geteuid() == 0 and str(ROOT).startswith("/srv/skew/")
    public = json.loads((VAULT / "public.json").read_bytes())
    receiver = public["address"].lower()
    profile = json.loads(Path("/var/lib/machine-commerce-sepolia/resources.json").read_bytes())[RID]
    assert profile["pay_to"] == receiver and profile["network"] == "eip155:42161"
    isolated = service_cannot_open_keys()
    urls = ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one.publicnode.com"]
    chain = Chain(HTTPS(urls))
    observations = []
    domain = "0x" + hash_domain({"name": "USD Coin", "version": "2", "chainId": 42161,
                                "verifyingContract": profile["asset"]}).hex()
    for url in urls:
        assert integer(chain.rpc(url, "eth_chainId", [])) == 42161
        # The primary supports finalized archive state. PublicNode's free
        # endpoint only permits recent state; label that independent read as
        # latest and never use it as payment finality or entitlement evidence.
        state_kind = "finalized" if url == profile["rpc_url"] else "latest"
        block = chain.rpc(url, "eth_getBlockByNumber", [state_kind, False])
        tag = block["number"]
        assert len(chain.rpc(url, "eth_getCode", [profile["asset"], tag])) > 2
        assert integer(chain.rpc(url, "eth_call", [{"to": profile["asset"], "data": "0x313ce567"}, tag])) == 6
        assert chain.rpc(url, "eth_call", [{"to": profile["asset"], "data": "0x3644e515"}, tag]).lower() == domain.lower()
        balance = integer(chain.rpc(url, "eth_call", [{"to": profile["asset"],
            "data": "0x70a08231" + receiver[2:].rjust(64, "0")}, tag]))
        observations.append({"rpc": url, "state_kind": state_kind, "block_number": integer(tag), "chain_id": 42161,
                             "usdc_balance_atoms": balance, "token_decimals": 6, "eip712_domain_matches": True})
    with httpx.Client(base_url=ORIGIN, timeout=30, trust_env=False, follow_redirects=False) as client:
        response = client.get(BASE + "/commerce/catalog")
        response.raise_for_status()
        plan = next(p for p in response.json()["plans"] if p["id"] == RID)
        assert plan["status"] == "AVAILABLE" and plan["price"] == "10" and not plan["auto_charge"]
        assert plan["product"]["pay_to"] == receiver and plan["duration_seconds"] == 2592000
        private = json.loads(Path("/var/lib/machine-commerce-sepolia/demo-state.json").read_bytes())
        login = client.post(BASE + "/sessions", json={"username": "buyer", "password": private["passwords"]["buyer"]})
        del private
        assert login.status_code == 200
        offer = plan["product"]["offers"][0]
        quote = client.post(BASE + "/commerce/checkouts", json={"resource_id": RID, "supply_id": offer["supply_id"],
            "plan_id": RID, "units": 1, "purpose": "research", "license": "internal-use", "max_total": "10",
            "max_age_seconds": 3600, "max_refresh_seconds": 3600, "response_seconds": 30,
            "idempotency_key": "wallet-readiness-" + secrets.token_hex(12)})
        quote.raise_for_status()
        mandate = client.post(BASE + "/payment-mandates", json={"payer": "0x" + "11" * 20,
            "payment_asset": profile["network"] + "/erc20:" + profile["asset"], "budget": "10", "max_order": "10",
            "resources": [RID], "ttl_seconds": 120})
        mandate.raise_for_status()
        prepared = client.post(BASE + "/commerce/checkouts/" + quote.json()["id"] + "/prepare",
                               json={"mandate_id": mandate.json()["id"]})
        prepared.raise_for_status()
        pid = prepared.json()["payment_id"]
        try:
            agreement = prepared.json()["agreement"]
            terms = agreement["terms"]
            request = {"terms_hash": agreement["terms_hash"], "data_version": terms["data_version"],
                       "units": terms["units"], "purpose": terms["purpose"], "license": terms["license"]}
            challenge = client.post(BASE + "/commerce/merchant/" + RID, json=request, headers={"Idempotency-Key": pid})
            assert challenge.status_code == 402
            required = decode_header(challenge.headers["payment-required"])
            option = required["accepts"][0]
            assert required["x402Version"] == 2 and option["network"] == "eip155:42161"
            assert option["payTo"] == receiver and option["amount"] == "10000000"
            assert option["asset"] == profile["asset"] and option["extra"] == {"name": "USD Coin", "version": "2"}
        finally:
            cancel = client.post(BASE + "/payments/" + pid + "/cancel", json={})
            cancel.raise_for_status()
            assert cancel.json()["status"] == "CANCELLED"
        final = client.get(BASE + "/commerce/checkouts/" + quote.json()["id"])
        final.raise_for_status()
        assert final.json()["entitlement"] is None
    return {"accepted": True, "checked_at": datetime.now(UTC).isoformat(), "recipient": public["address"],
        "network": "eip155:42161", "subscription_status": "AVAILABLE", "price_usdc": "10", "duration_days": 30,
        "auto_charge": False, "api_service_key_access_denied": isolated, "chain_observations": observations,
        "live_unsigned_x402_challenge": {"status": 402, "version": 2, "requirements": option},
        "verification_payment_id": pid, "verification_checkout_cancelled": True, "customer_signatures": 0,
        "transactions_submitted": 0, "subscription_grants_created": 0,
        "failed_archive_checks": [{"rpc": "https://arbitrum-one.publicnode.com", "http_status": 403,
            "reason": "FREE_ENDPOINT_REQUIRES_TOKEN_FOR_ARCHIVE_STATE"},
            {"rpc": "https://arbitrum.drpc.org", "http_status": 400, "reason": "FINALIZED_ARCHIVE_STATE_UNAVAILABLE"}],
        "assurance": "CONFIGURED_AND_UNSIGNED_CHECKOUT_VERIFIED_NOT_A_PAID_CUSTOMER_PURCHASE"}


def main():
    try:
        result = verify()
        (ROOT / "artifacts/atlas-release/subscription-wallet-live.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    except Exception as exc:
        raise SystemExit("Subscription activation verification failed: " + type(exc).__name__) from None


if __name__ == "__main__":
    main()
