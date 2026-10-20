#!/usr/bin/env python3
"""Read-only live WS probe with explicitly synthetic Muse response fixtures.

Never passes --live, reads a key, calls a model, or submits an exchange order.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"src"))
from economic_machine.muse_maker import MODEL, response
from economic_machine.muse_policy_worker import ForwardGate, atomic, now_ms, packet, read_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, required=True); p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(); os.umask(0o077)
    args.out.mkdir(parents=True, exist_ok=False); control = args.out/"control"; control.mkdir()
    with (args.out/"process.log").open("w") as log:
        child = subprocess.Popen([str(args.binary.resolve()), "--observe", str(args.out.resolve()), str(control.resolve()), "45"], stdout=log, stderr=log)
        published = []
        try:
            start = None
            for _ in range(200):
                if child.poll() is not None: raise RuntimeError("observation process exited during startup")
                if (args.out/"session-start.json").exists(): start = read_json(args.out/"session-start.json"); break
                time.sleep(0.1)
            if start is None: raise RuntimeError("observation startup deadline")
            for version, spread in ((1, 1000), (2, 2000)):
                issued = now_ms(); gate = ForwardGate(issued)
                while not gate.ready() and now_ms()-issued < 25000:
                    if child.poll() is not None: raise RuntimeError("observation process exited")
                    try:
                        status = read_json(args.out/"status.json"); b = status["public_book"]; stamp = b["source_ms"]
                        # Convert only actual public BBO scalars to the context
                        # test shape. This is not a Japanese Geyser observation.
                        obs = {"updated_wall_ms": stamp, "archive_failed": False, "onchain_pools_fresh": 0,
                            "model": {"at_wall_ms": stamp, "version": stamp, "fair": (b["bid"]+b["ask"])/2,
                                      "sigma_bps": 1, "flow": 0, "onchain_available": False}}
                        gate.add(obs, now_ms())
                    except (OSError, ValueError, KeyError): pass
                    time.sleep(0.1)
                policy = {"mode": "quote", "half_spread_millibps": spread, "inventory_skew_millibps": 5000,
                    "flow_shift_millibps": 0, "volatility_multiplier_milli": 1000, "clip_usd_micros": 600000000,
                    "refresh_ticks": 2, "ttl_ms": 60000}
                content = {"policy": policy, "context_sha256": "a"*64, "evidence_features": ["volatility"]}
                fixture = {"model": MODEL, "provider": "Meta", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}
                proposal = response(json.dumps(fixture), "a"*64)
                install = packet(proposal, session_id=start["session_id"], version=version, issued_ms=issued,
                    published_ms=now_ms(), deadline_ms=start["deadline_ms"], gate=gate, kind="fixture")
                atomic(control/"policy.json", install); published.append(install)
                time.sleep(2)
            child.wait(timeout=60)
            if child.returncode: raise RuntimeError("observation connection failed")
            summary = read_json(args.out/"summary.json")
            assert summary["policies_installed"] == 2 and summary["native_plans"] > 0
            assert summary["real_order_attempts"] == 0 and summary["attributed_fills"] == 0
            result = {"passed": True, "source": "actual_Pacifica_public_WebSocket", "policy_origin": "synthetic_response_fixtures",
                "actual_model_responses": 0, "actual_model_calls": 0, "credentials_read": False, "orders_submitted": 0,
                "installed_policy_versions": [x["version"] for x in published], "summary": summary}
            atomic(args.out/"verification.json", result); print(json.dumps(result))
        finally:
            if child.poll() is None: child.terminate(); child.wait(timeout=30)


if __name__ == "__main__": main()
