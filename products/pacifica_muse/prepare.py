#!/usr/bin/env python3
"""Prepare a Muse request from public observer status; never contacts a model."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from economic_machine.muse_maker import canonical, digest, request, response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--now-ms", type=int, help="Replay clock; omit for a current observation")
    parser.add_argument("--response", type=Path, help="Validate an existing provider response, without another call")
    args = parser.parse_args()
    os.umask(0o077)
    args.out.mkdir(parents=True, exist_ok=False)
    observer = json.loads(args.observer.read_text())
    req = request(observer, args.now_ms if args.now_ms is not None else time.time_ns()//1_000_000)
    frame = json.loads(req["messages"][1]["content"])
    (args.out / "request.json").write_text(canonical(req) + "\n")
    (args.out / "context.json").write_text(canonical(frame) + "\n")
    if args.response:
        proposal = response(args.response.read_text(), frame["context_sha256"])
        (args.out / "candidate.json").write_text(canonical(proposal) + "\n")
    manifest = {"request_sha256": digest(req), "context_sha256": frame["context_sha256"],
                "model_calls": 0, "order_submissions": 0, "services_changed": False,
                "response_validated": args.response is not None}
    (args.out / "manifest.json").write_text(canonical(manifest) + "\n")
    print(canonical(manifest))


if __name__ == "__main__":
    main()
