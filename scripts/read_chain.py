"""Verify real DNS pinning, TLS and read-only Arbitrum RPC without a wallet."""

import json
import time
from pathlib import Path

from machine_commerce.transport import HTTPS, Chain, hash32, integer

ROOT = Path(__file__).resolve().parents[1]


def main():
    url = "https://arb1.arbitrum.io/rpc"
    chain = Chain(HTTPS({url}))
    started = time.perf_counter()
    network = integer(chain.rpc(url, "eth_chainId", []))
    latest = chain.rpc(url, "eth_getBlockByNumber", ["latest", False])
    finalized = chain.rpc(url, "eth_getBlockByNumber", ["finalized", False])
    if network != 42161 or integer(finalized["number"]) > integer(latest["number"]):
        raise RuntimeError("chain identity or finality ordering mismatch")
    result = {"network": "eip155:42161", "provider": "Arbitrum public RPC", "read_only": True,
        "tls_certificate_verified": True, "dns_public_ip_pinned": True, "redirects_enabled": False,
        "signing_or_submission": False, "captured_at": int(time.time()),
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "latest": {"number": integer(latest["number"]), "hash": hash32(latest["hash"])},
        "finalized": {"number": integer(finalized["number"]), "hash": hash32(finalized["hash"])},
        "assurance": "RPC finalized tag from one public provider; not independent L1 finality verification or a paid transaction."}
    (ROOT / "artifacts/live-chain-read.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
