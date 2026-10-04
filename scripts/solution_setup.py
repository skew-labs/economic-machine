"""Read-only wallet quotes and exact VRF administration intents. Never signs or sends."""

import argparse
import json
import urllib.request
from pathlib import Path

from solution_mainnet_preflight import deployment_review, inspect_network

from economic_machine.values import MachineError, digest
from machine_engine.solution_chain import FinalizedChain, ReadRPC, quantity
from machine_engine.solution_setup import (
    activate,
    add_consumer,
    address,
    create_subscription,
    fund_subscription,
    match_runtime,
    verify_mining_configuration,
)

ROOT = Path(__file__).resolve().parents[1]


class AdminReadRPC(ReadRPC):
    def __call__(self, method, params):
        if method not in {"eth_estimateGas", "eth_gasPrice", "eth_getBalance", "eth_getTransactionCount"}:
            return super().__call__(method, params)
        self.counter += 1
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}
            ).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "SKEW-solution-setup-read/1.0"},
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                data = response.read(131073)
            if len(data) > 131072:
                raise MachineError("SOLUTION_SETUP_RPC_RESPONSE_BOUND")
            raw = json.loads(data)
            if (
                raw.get("id") != self.counter
                or raw.get("jsonrpc") != "2.0"
                or "error" in raw
                or "result" not in raw
            ):
                raise MachineError("SOLUTION_SETUP_RPC_INVALID_RESPONSE")
            return raw["result"]
        except (OSError, ValueError):
            raise MachineError("SOLUTION_SETUP_RPC_UNAVAILABLE") from None


def quote(first, second, payload, maximum_gas_wei):
    if type(maximum_gas_wei) is not int or not 0 < maximum_gas_wei < 2**128:
        raise MachineError("SOLUTION_SETUP_GAS_BUDGET_REQUIRED")
    tx = payload["transaction"] if "transaction" in payload else payload["unsigned_transaction"]
    observed = []
    for provider in [first, second]:
        if quantity(provider("eth_chainId", [])) != tx["chainId"]:
            raise MachineError("SOLUTION_WRONG_CHAIN")
        pending = quantity(provider("eth_getTransactionCount", [tx["from"], "pending"]))
        latest = quantity(provider("eth_getTransactionCount", [tx["from"], "latest"]))
        if pending != latest:
            raise MachineError("SOLUTION_SETUP_PENDING_WALLET_TRANSACTION")
        request = {k: v for k, v in tx.items() if k not in {"chainId"}}
        observed.append(
            {
                "provider": provider.identity,
                "nonce": pending,
                "balance_wei": str(quantity(provider("eth_getBalance", [tx["from"], "latest"]))),
                "gas_estimate": quantity(provider("eth_estimateGas", [request])),
                "gas_price_wei": quantity(provider("eth_gasPrice", [])),
            }
        )
    if observed[0]["nonce"] != observed[1]["nonce"]:
        raise MachineError("SOLUTION_SETUP_NONCE_DISAGREEMENT")
    gas = (max(o["gas_estimate"] for o in observed) * 120 + 99) // 100
    price = max(o["gas_price_wei"] for o in observed) * 2
    if gas <= 0 or price <= 0 or gas * price > maximum_gas_wei:
        raise MachineError("SOLUTION_SETUP_GAS_QUOTE_OVER_BUDGET")
    result = {
        "intent": payload,
        "observations": observed,
        "unsigned_transaction": tx | {"nonce": observed[0]["nonce"], "gas": gas, "gasPrice": price},
        "maximum_gas_wei": str(gas * price),
        "configured_gas_cap_wei": str(maximum_gas_wei),
        "balance_sufficient": min(int(o["balance_wei"]) for o in observed) >= gas * price,
        "fresh_owner_approval_required": True,
        "signed": False,
        "sent": False,
    }
    result["review_sha256"] = digest(result)
    return result


def load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 16384:
        raise MachineError("SOLUTION_SETUP_PRIVATE_CONFIG_REQUIRED")
    raw = json.loads(path.read_text())
    required = {"rpc_a", "rpc_b", "chain_id", "owner", "maximum_gas_wei"}
    optional = {
        "subscription",
        "mining",
        "code_sha256",
        "minimum_link_reserve_juels",
        "fund_link_juels",
        "maximum_link_juels",
    }
    if required - set(raw) or set(raw) - required - optional:
        raise MachineError("SOLUTION_SETUP_CONFIG_FIELDS")
    address(raw["owner"])
    return raw


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote read/verification required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "action", choices=["create-subscription", "deploy", "register-consumer", "fund", "activate", "verify"]
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = load(args.config)
    first, second = AdminReadRPC(config["rpc_a"]), AdminReadRPC(config["rpc_b"])
    owner = config["owner"]
    chain = config["chain_id"]
    sub = config.get("subscription")
    read = inspect_network(first, second, chain, sub)
    if args.action == "create-subscription":
        if sub is not None:
            raise MachineError("SOLUTION_SETUP_SUBSCRIPTION_ALREADY_CONFIGURED")
        payload = create_subscription(owner, chain_id=chain)
    else:
        if read["subscription"] is None or read["subscription"]["owner"].lower() != owner.lower():
            raise MachineError("SOLUTION_SETUP_SUBSCRIPTION_OWNER_REQUIRED")
        if args.action == "deploy":
            payload = deployment_review(owner, sub, config["minimum_link_reserve_juels"], chain_id=chain)
        else:
            reader = FinalizedChain(first, second, config["mining"], config["code_sha256"], chain_id=chain)
            verified = verify_mining_configuration(reader, owner, sub, config["minimum_link_reserve_juels"])
            # Verify the build/source pins, then exact runtime templates and all immutable values.
            deployment_review(owner, sub, config["minimum_link_reserve_juels"], chain_id=chain)
            compiled = json.loads((ROOT / "artifacts/solution-mainnet/contracts.json").read_text())
            for target, name in [
                (reader.contract, "SkewSolutionMining"),
                (verified["reward"]["token"], "SkewSolutionToken"),
            ]:
                code = reader.pair("eth_getCode", [target, hex(verified["anchor"]["number"])])
                match_runtime(bytes.fromhex(code[2:]), compiled[name])
            consumers = {v.lower() for v in read["subscription"]["consumers"]}
            if args.action == "register-consumer":
                if config["mining"].lower() in consumers:
                    raise MachineError("SOLUTION_SETUP_CONSUMER_ALREADY_PRESENT")
                payload = add_consumer(owner, sub, config["mining"], chain_id=chain)
            elif args.action == "fund":
                payload = fund_subscription(
                    owner,
                    sub,
                    config["fund_link_juels"],
                    approved_link_cap=config["maximum_link_juels"],
                    existing_balance=int(read["subscription"]["link_balance_juels"]),
                    chain_id=chain,
                )
                balance = reader.call(
                    verified["anchor"],
                    "balanceOf(address)",
                    ["address"],
                    [address(owner)],
                    ["uint256"],
                    target=read["contracts"]["link"]["address"],
                )[0]
                if balance < config["fund_link_juels"]:
                    raise MachineError("SOLUTION_SETUP_LINK_BALANCE_INSUFFICIENT")
            elif args.action == "activate":
                if (
                    verified["activated"]
                    or not verified["admission_paused"]
                    or config["mining"].lower() not in consumers
                    or int(read["subscription"]["link_balance_juels"]) < config["minimum_link_reserve_juels"]
                ):
                    raise MachineError("SOLUTION_SETUP_ACTIVATION_PRECONDITIONS")
                payload = activate(owner, config["mining"], chain_id=chain)
            else:
                payload = verified
    result = (
        {"network_read": read, "setup": payload}
        if args.action == "verify"
        else quote(first, second, payload, config["maximum_gas_wei"]) | {"network_read": read}
    )
    destination = Path(args.output)
    # Review material is public metadata; an existing approved review is never overwritten.
    with destination.open("x") as out:
        out.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(destination), "signed": False, "sent": False, "action": args.action}))


if __name__ == "__main__":
    main()
