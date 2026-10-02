"""Remote-only real HTTP/scheduler restart proof; public test wallet reads only."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Run verification on the authorized remote compute host")
    token = secrets.token_urlsafe(40)
    env = os.environ | {"ENGINE_ADMIN_TOKEN": token, "ENGINE_ALLOW_LIVE_TRADING": "0"}
    observations = []
    with tempfile.TemporaryDirectory(prefix="machine-sync-proof-") as directory:
        command = [
            sys.executable,
            "-m",
            "economic_machine.cli",
            "serve",
            "--db",
            directory + "/engine.sqlite3",
            "--port",
            "44880",
        ]
        client = httpx.Client(
            base_url="http://127.0.0.1:44880", headers={"Authorization": "Bearer " + token}, timeout=40
        )
        processes = []
        try:

            def start():
                child = subprocess.Popen(
                    command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                processes.append(child)
                for _ in range(100):
                    if child.poll() is not None:
                        raise RuntimeError("isolated runtime failed to start")
                    try:
                        response = client.get("/api/engine/overview")
                        response.raise_for_status()
                        return child
                    except httpx.ConnectError:
                        time.sleep(0.1)
                raise RuntimeError("isolated runtime startup deadline")

            child = start()
            # This public address is the already-published disposable test buyer.
            response = client.post(
                "/api/engine/connections",
                json={
                    "name": "Public disposable test buyer",
                    "profile": "arbitrum-sepolia-wallet",
                    "config": {"address": "0x24d0B9Bc844754Dd1b22f215EBb06627f7314E79"},
                },
            )
            response.raise_for_status()
            cid = response.json()["id"]
            response = client.post(
                f"/api/engine/connections/{cid}/schedule", json={"enabled": True, "interval_seconds": 15}
            )
            response.raise_for_status()

            def await_read(after=0):
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    data = client.get("/api/engine/overview").json()
                    connection = data["connections"][0]
                    if connection["status"] == "CONNECTED" and connection["observed_at"] > after:
                        observations.append(
                            {
                                "observed_at": connection["observed_at"],
                                "block_number": connection["snapshot"]["block_number"],
                                "assets": data["assets"],
                                "sync_jobs": data["sync_jobs"],
                                "live_transmission_enabled": data["trading"]["live_transmission_enabled"],
                            }
                        )
                        return connection["observed_at"]
                    time.sleep(0.5)
                raise RuntimeError("public wallet scheduler read deadline")

            first = await_read()
            child.terminate()
            child.wait(timeout=20)
            start()
            second = await_read(first)
            report = {
                "checked_at": int(time.time()),
                "runtime": "REAL_UVICORN_LOOPBACK_HTTP",
                "scheduler_runs": len(observations),
                "restart_preserved_schedule": second > first,
                "credentials": "GENERATED_LOCAL_OWNER_TOKEN_NOT_PERSISTED",
                "financial_transmissions": 0,
                "language_model_calls": 0,
                "observations": observations,
            }
            (ROOT / "artifacts/engine-release/runtime-sync-proof.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps({k: v for k, v in report.items() if k != "observations"}))
        finally:
            client.close()
            for child in processes:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=20)


if __name__ == "__main__":
    main()
