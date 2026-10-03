"""Provision an owner-approved public recipient. Never generates wallet keys.

Run on the trusted remote host, then load both generated registries in the
same service revision. Dry-run is default; --apply writes the two registries.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

from economic_machine.values import MachineError
from machine_commerce.checkout import DEFAULT_PLANS, USDC_ASSETS, validate_plans
from machine_commerce.payments import validate_profiles
from machine_commerce.transport import https_url
from machine_commerce.x402 import address


def configuration(resources, merchants, recipient, origin, rpc_url, network="eip155:42161"):
    recipient = address(recipient)
    if recipient == "0x" + "0" * 40:
        raise MachineError("owner-approved nonzero public recipient required")
    parsed = https_url(origin)
    if parsed.path or network not in USDC_ASSETS:
        raise MachineError("exact HTTPS origin and approved Arbitrum network required")
    https_url(rpc_url)
    rid = "atlas-monthly"
    profile = {"url": origin + "/commerce/api/commerce/merchant/" + rid, "network": network,
        "asset": USDC_ASSETS[network], "pay_to": recipient, "token_name": "USD Coin", "token_version": "2",
        "max_timeout_seconds": 120, "seller_owner": "merchant-" + rid, "data_type": "subscription." + rid,
        "data_version": "monthly-v1", "rpc_url": rpc_url, "finality": "finalized"}
    if rid in resources and resources[rid] != profile:
        raise MachineError("existing subscription differs; explicit migration required")
    registry = validate_profiles(resources | {rid: profile})
    validate_plans(DEFAULT_PLANS, registry)
    merchant = {"facilitator_url": "https://facilitator.payai.network"}
    if rid in merchants and merchants[rid] != merchant:
        raise MachineError("existing facilitator differs; explicit migration required")
    return registry, merchants | {rid: merchant}


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        folder = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(folder)
        finally: os.close(folder)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", required=True, help="Public receiving address, never a private key")
    parser.add_argument("--origin", default="https://machine.148-113-153-116.nip.io")
    parser.add_argument("--rpc", default="https://arb1.arbitrum.io/rpc")
    parser.add_argument("--network", default="eip155:42161", choices=sorted(USDC_ASSETS))
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--merchants", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not str(Path(__file__).resolve()).startswith("/srv/skew/"):
        parser.error("provisioning runs only on the trusted remote host")
    if args.resources.resolve() == args.merchants.resolve():
        parser.error("separate resource and merchant registries required")
    original = {p: p.read_bytes() if p.exists() else None for p in [args.resources, args.merchants]}
    resources, merchants = configuration(*(json.loads(original[p]) if original[p] else {} for p in original),
        args.recipient, args.origin, args.rpc, args.network)
    if args.apply:
        try:
            for path, value in [(args.resources, resources), (args.merchants, merchants)]:
                atomic_write(path, (json.dumps(value, indent=2) + "\n").encode())
        except BaseException:
            for path, value in original.items():
                if value is None: path.unlink(missing_ok=True)
                else: atomic_write(path, value)
            raise
    print(json.dumps({"applied": args.apply, "resource_id": "atlas-monthly", "network": args.network,
        "recipient": address(args.recipient), "price_usdc": "10", "duration_days": 30,
        "auto_charge": False, "wallet_keys_generated": 0, "payments_sent": 0}))


if __name__ == "__main__":
    main()
