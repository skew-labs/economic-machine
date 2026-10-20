#!/usr/bin/env python3
"""Read-only account/service inspection. No signer, POST, stop or restart."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from economic_machine.muse_maker import handover_readiness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--old-service", default="pacifica-ticking-agent.service")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", args.account):
        parser.error("invalid public account address")
    if not re.fullmatch(r"[a-z0-9-]+\.service", args.old_service):
        parser.error("invalid service name")
    query = urllib.parse.urlencode({"account": args.account})

    def snapshot():
        def fetch(item):
            key, path = item
            with urllib.request.urlopen("https://api.pacifica.fi/api/v1/"+path+"?"+query, timeout=10) as resp:
                body = resp.read(1024*1024+1)
                if len(body) > 1024*1024:
                    raise ValueError("response exceeds bound")
                return key, json.loads(body)
        with ThreadPoolExecutor(max_workers=4) as pool:
            value = dict(pool.map(fetch, [("account", "account"), ("positions", "positions"),
                                          ("orders", "orders"), ("settings", "account/settings")]))
        value["received_ms"] = time.time_ns()//1_000_000
        return value

    before = snapshot()
    state = subprocess.run(["systemctl", "show", args.old_service, "-p", "ActiveState", "--value"],
                           text=True, capture_output=True, check=True).stdout.strip()
    after = snapshot()
    now = time.time_ns()//1_000_000
    report = {"asof_ms": now, "account": args.account, "old_service": args.old_service,
              "old_service_state": state, "before": before, "after": after,
              "handover": handover_readiness(before, after, state, now),
              "financial_actions": 0, "credentials_read": False, "services_changed": False}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        handle.write(json.dumps(report, indent=2)+"\n")
    print(json.dumps({"asof_ms": now, "old_service_state": state, "handover": report["handover"],
                      "financial_actions": 0}))


if __name__ == "__main__":
    main()
