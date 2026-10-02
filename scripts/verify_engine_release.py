"""Remote CLI/API smoke check in a disposable owner DB, optionally public wallet reads."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-wallet-read", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = secrets.token_urlsafe(48)
    env = os.environ | {"ENGINE_ADMIN_TOKEN": token, "PYTHONPATH": str(ROOT / "src")}
    with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        database = Path(directory) / "owner.sqlite3"
        command = [sys.executable, "-m", "economic_machine.cli", "serve", "--db", str(database), "--port", str(port)]
        with open(Path(directory) / "server.log", "w") as log:
            process = subprocess.Popen(command, env=env, cwd=ROOT, stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
                    ready = False
                    for _ in range(40):
                        try:
                            if client.get("/engine").status_code == 200:
                                ready = True
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    if not ready:
                        raise RuntimeError("CLI_SERVER_NOT_READY")
                    headers = {"Authorization": "Bearer " + token}
                    assert client.get("/api/engine/overview").status_code == 401
                    assert client.get("/api/engine/overview", headers=headers | {"Origin": "https://example.org"}).status_code == 403
                    overview = client.get("/api/engine/overview", headers=headers).json()
                    assert overview["mode"] == "SELF_HOSTED" and overview["connections"] == []
                    assert database.stat().st_mode & 0o777 == 0o600
                    fixture = json.loads((ROOT / "cases/economic_program_demo.json").read_text())
                    compiled = client.post("/api/engine/programs/compile", json=fixture, headers=headers).json()
                    assert compiled["execution_authority"] == "NONE"
                    checks = {"cli_server": True, "owner_auth": True, "origin_boundary": True,
                              "private_database": True, "policy_compile": True,
                              "program_hash": compiled["program"]["program_hash"],
                              "financial_submission": False, "signatures_created": 0}
                    if args.public_wallet_read:
                        connection = client.post("/api/engine/connections", headers=headers, json={
                            "name": "Public disposable test buyer", "profile": "arbitrum-sepolia-wallet",
                            "config": {"address": "0x24d0B9Bc844754Dd1b22f215EBb06627f7314E79"}}).json()
                        refreshed = client.post(f"/api/engine/connections/{connection['id']}/sync", json={}, headers=headers).json()
                        assert refreshed["status"] == "CONNECTED", refreshed.get("error_code")
                        overview = client.get("/api/engine/overview", headers=headers).json()
                        checks["live_public_wallet"] = {"assets": overview["assets"],
                            "snapshot": overview["connections"][0]["snapshot"], "read_only": True}
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(checks, indent=2) + "\n")
                    print(json.dumps({"verified": True, "public_wallet_read": args.public_wallet_read,
                                      "financial_submission": False, "output": str(args.output)}))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
