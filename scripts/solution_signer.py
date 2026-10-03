"""Owner-local keystore signer; no worker or public server ever receives a key."""

import argparse
import getpass
import json
import ssl
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from eth_account import Account

from economic_machine.values import MachineError
from machine_engine.solution_chain import FinalizedChain, ReadRPC
from machine_engine.solution_signer import OwnerOutbox


def private_json(path, bound=65536):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > bound:
        raise MachineError("SOLUTION_SIGNER_PRIVATE_FILE_REQUIRED")
    return json.loads(path.read_text())


def send_once(url, raw):
    # Exact single submission, no HTTP redirect, no library retry and no arbitrary RPC.
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MachineError("SOLUTION_SIGNER_PRIVATE_HTTPS_RPC_REQUIRED")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise MachineError("SOLUTION_SIGNER_RPC_REDIRECT")

    request = urllib.request.Request(
        url,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw]}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    with opener.open(request, timeout=15) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise MachineError("SOLUTION_SIGNER_RPC_RESPONSE_BOUND")
    answer = json.loads(data)
    if answer.get("id") != 1 or answer.get("error") or not isinstance(answer.get("result"), str):
        raise MachineError("SOLUTION_SIGNER_RPC_REJECTED")
    return answer["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sign = sub.add_parser("sign")
    sign.add_argument("--intent", required=True)
    sign.add_argument("--fees", required=True)
    sign.add_argument("--keystore", required=True)
    send = sub.add_parser("broadcast")
    send.add_argument("--id", required=True)
    send.add_argument("--approve-tx-hash", required=True)
    check = sub.add_parser("reconcile")
    check.add_argument("--id", required=True)
    args = parser.parse_args()
    config = private_json(args.config)
    required = {
        "rpc_a",
        "rpc_b",
        "chain_id",
        "contract",
        "code_sha256",
        "owner",
        "directory",
        "per_tx_limit_wei",
        "daily_limit_wei",
    }
    if set(config) != required:
        raise MachineError("SOLUTION_SIGNER_CONFIG_FIELDS")
    reader = FinalizedChain(
        ReadRPC(config["rpc_a"]),
        ReadRPC(config["rpc_b"]),
        config["contract"],
        config["code_sha256"],
        chain_id=config["chain_id"],
    )
    outbox = OwnerOutbox(
        config["directory"],
        reader,
        config["owner"],
        per_tx_limit=config["per_tx_limit_wei"],
        daily_limit=config["daily_limit_wei"],
    )
    try:
        if args.command == "sign":
            # Password prompt never goes into CLI arguments, environment, workers, logs or persistence.
            keystore = private_json(args.keystore)
            key = Account.decrypt(keystore, getpass.getpass("Local keystore password: "))
            try:
                result = outbox.sign(private_json(args.intent), private_json(args.fees), key)
            finally:
                del key
        elif args.command == "broadcast":
            result = outbox.broadcast(
                args.id, lambda raw: send_once(config["rpc_a"], raw), approved_hash=args.approve_tx_hash
            )
        else:
            result = outbox.reconcile(args.id)
        print(json.dumps(result))
    finally:
        outbox.close()


if __name__ == "__main__":
    main()
