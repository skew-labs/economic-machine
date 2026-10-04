"""Focused Canadian-host release. Never reads secrets or signs transactions.

Run only after focused tests. Saves previous sources/configs before activation.
The unrelated, uncommitted mining/DataPass work is deliberately not deployed.
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

BUILD = Path(__file__).resolve().parents[1]
LIVE = Path("/srv/skew/economic-machine-commerce-20261002")
TAG = "fuel-20261004"
# Exact deployed baseline; fail rather than overwrite another release's changes.
EXPECTED = {
    "src/machine_engine/routes.py": "b8d4c45dd529427f741373ec5db2c46cc6098ca1c2b325e31fa50c11e275612a",
    "src/machine_engine/connections.py": "c946d307ab87a568bd4a7170d4d65a527a171329421d6963f6b2ef08e2bf70cf",
    "src/machine_commerce/access.py": "6f5f1f4dcea9c89b79287d7f72fffe9bf78ce50e1e3bed93ae089e99ff02d6a1",
    "web/index.html": "92f60448bfa9c05e8baae8371c6944aa48603ed4c8c7ad2cb9a8fefac455a8b7",
}
NEW = ["src/machine_commerce/gas_router.py", "src/machine_commerce/gas_portal.py",
       "src/machine_commerce/fuel_price.py", "src/machine_engine/fuel.py", "src/machine_engine/fuel_routes.py"]
WEB = ["web/"+name for name in ["swap.html", "swap.js", "swap-wallet.js", "swap.css", "swap-crypto.js"]]


def run(*args):
    subprocess.run(args, check=True)


def main():
    if BUILD != LIVE/"build/datapass-mining-pr9" or sys.argv[1:] != ["--activate"]:
        raise SystemExit("Use the authorized isolated builder and explicit --activate.")
    for name, sha in EXPECTED.items():
        if hashlib.sha256((LIVE/name).read_bytes()).hexdigest() != sha:
            raise SystemExit("Baseline changed: "+name)
    release, backup = LIVE/"releases"/TAG, LIVE/"release-backups"/TAG
    if release.exists() or backup.exists():
        raise SystemExit("Release already exists. Inspect it; do not overwrite a previous activation.")
    backup.mkdir(parents=True)
    release.mkdir(parents=True)
    for name in EXPECTED:
        dest = backup/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LIVE/name, dest)
    # Seed from the deployed baseline, not the unrelated working tree changes.
    shutil.copytree(LIVE/"src", release/"src", ignore=shutil.ignore_patterns("__pycache__"))
    for name in list(EXPECTED)+NEW+WEB:
        dest = release/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BUILD/name, dest)
        dest.chmod(0o644)
    run("sudo", "cp", "/etc/nginx/sites-available/machine", str(backup/"machine.nginx"))
    run("sudo", "install", "-m", "644", str(BUILD/"infra/machine-commerce-fuel.service"), "/etc/systemd/system/machine-commerce-fuel.service")
    run("sudo", "install", "-m", "644", str(BUILD/"infra/fuel-rate.nginx.conf"), "/etc/nginx/conf.d/machine-fuel-rate.conf")
    run("sudo", "install", "-m", "644", str(BUILD/"infra/fuel.nginx.conf"), "/etc/nginx/machine-commerce-snippets/fuel.conf")
    conf = (backup/"machine.nginx").read_text()
    needle = "  include /etc/nginx/machine-commerce-snippets/portal.conf;"
    if conf.count(needle) != 1:
        raise SystemExit("Unexpected nginx baseline; activation not started.")
    staged = backup/"machine.fuel.nginx"
    staged.write_text(conf.replace(needle, "  include /etc/nginx/machine-commerce-snippets/fuel.conf;\n"+needle))
    run("sudo", "install", "-m", "644", str(staged), "/etc/nginx/sites-available/machine")
    run("sudo", "nginx", "-t")
    run("sudo", "systemctl", "daemon-reload")
    run("sudo", "systemctl", "enable", "--now", "machine-commerce-fuel.service")
    # Core API gets only the tested engine/Fuel connection delta.
    for name in list(EXPECTED)+NEW:
        shutil.copy2(release/name, LIVE/name)
    run("sudo", "systemctl", "restart", "machine-commerce-sepolia-runtime.service")
    run("sudo", "systemctl", "reload", "nginx")
    manifest = {"release": TAG, "baseline": EXPECTED, "backup": str(backup),
        "deployed_sha256": {name: hashlib.sha256((release/name).read_bytes()).hexdigest() for name in list(EXPECTED)+NEW+WEB},
        "configuration_sha256": {name: hashlib.sha256((BUILD/name).read_bytes()).hexdigest()
            for name in ["infra/machine-commerce-fuel.service", "infra/fuel.nginx.conf", "infra/fuel-rate.nginx.conf"]},
        "transactions_broadcast": 0, "wallet_keys_read": 0}
    (BUILD/"artifacts/fuel/deployment.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps({"release": TAG, "status": "ACTIVATED_VERIFY_HTTP_NEXT"}))


if __name__ == "__main__":
    main()
