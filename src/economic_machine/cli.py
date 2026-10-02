"""Self-hosted Economic Machine CLI. No signer or transaction sender is installed."""

import argparse
import json
import os
from pathlib import Path

from .compiler import compile_program
from .kernel import EconomicKernel
from .runtime import MachineRuntime
from .spec import SPEC, SPEC_HASH, conformance_vectors


def read(path):
    return json.loads(Path(path).read_text())


def main():
    # Owner state, account snapshots and SQLite journals are private by default.
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="economic-machine")
    parser.add_argument("command", choices=["serve", "compile", "run", "spec", "spec-vectors", "install-state",
        "register", "evaluate", "ingest", "status", "tick", "verify", "begin-execution", "pause", "resume"])
    parser.add_argument("--db", type=Path, default=Path("runtime/engine.sqlite3"))
    parser.add_argument("--program", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--delta", type=Path)
    parser.add_argument("--program-id")
    parser.add_argument("--event-id")
    parser.add_argument("--receipt-hash")
    parser.add_argument("--at")
    parser.add_argument("--reason", default="OWNER_REQUEST")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()

    def required(*fields):
        if any(getattr(args, field) is None for field in fields):
            parser.error("required: " + ", ".join("--" + field.replace("_", "-") for field in fields))

    if args.command == "serve":
        import uvicorn

        from machine_engine.api import create_engine_app
        if not 1024 <= args.port <= 65535:
            parser.error("--port must be between 1024 and 65535")
        uvicorn.run(create_engine_app(args.db, origin=f"http://127.0.0.1:{args.port}"),
                    host="127.0.0.1", port=args.port, access_log=False, limit_concurrency=32,
                    timeout_keep_alive=5)
        return
    if args.command == "spec":
        result = {"spec": SPEC, "spec_hash": SPEC_HASH}
    elif args.command == "spec-vectors":
        result = conformance_vectors()
    elif args.command == "compile":
        required("program")
        result = compile_program(read(args.program))
    elif args.command == "run":
        required("program", "state", "at")
        result = EconomicKernel().evaluate(compile_program(read(args.program)), read(args.state), at=args.at)
    else:
        runtime = MachineRuntime(args.db)
        if args.command == "install-state":
            required("state")
            result = runtime.install_state(read(args.state))
        elif args.command == "register":
            required("program")
            result = runtime.register_program(read(args.program))
        elif args.command == "evaluate":
            required("program_id", "at")
            result = runtime.evaluate(args.program_id, at=args.at)
        elif args.command == "ingest":
            required("delta", "event_id")
            result = runtime.ingest(read(args.delta), event_id=args.event_id)
        elif args.command == "tick":
            required("at")
            result = runtime.tick(at=args.at)
        elif args.command == "verify":
            required("receipt_hash")
            result = {"verified_replay": runtime.verify_receipt(args.receipt_hash)}
        elif args.command == "begin-execution":
            required("receipt_hash", "at")
            result = runtime.begin_execution(args.receipt_hash, at=args.at)
        elif args.command in {"pause", "resume"}:
            required("program_id")
            method = runtime.pause_program if args.command == "pause" else runtime.resume_program
            result = method(args.program_id, reason=args.reason)
        else:
            result = runtime.status()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
