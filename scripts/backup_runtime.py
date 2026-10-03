"""Root-operated backup/restore rehearsal on Canada; exports no key material."""

import argparse
import json
import os
from pathlib import Path

from machine_commerce.backup import restore, seal

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path("/var/lib/skew-treasury/runtime-backups")


def main():
    if os.geteuid() != 0 or not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Trusted remote root operation required")
    parser = argparse.ArgumentParser()
    parser.add_argument('--off-host-config')
    args = parser.parse_args()
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    if PRIVATE.is_symlink() or PRIVATE.stat().st_uid != 0 or PRIVATE.stat().st_mode & 0o777 != 0o700:
        raise SystemExit("Private backup directory required")
    key_path = PRIVATE / "encryption-key"
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(os.urandom(32))
            output.flush()
            os.fsync(output.fileno())
    if (key_path.is_symlink() or key_path.stat().st_uid != 0 or key_path.stat().st_nlink != 1
            or key_path.stat().st_mode & 0o777 != 0o600):
        raise SystemExit("Private backup key required")
    key = key_path.read_bytes()
    base = Path("/var/lib/machine-commerce-sepolia-runtime")
    databases = [base / "runtime.sqlite3", *sorted((base / "engine-workspaces").glob("*.sqlite3"))]
    config = Path("/var/lib/machine-commerce-sepolia")
    attachments = {name: config / name for name in ["operators.json", "resources.json", "merchants.json"]}
    if (config / 'compute.json').exists():
        attachments['compute.json'] = config / 'compute.json'
    vault = Path("/var/lib/skew-treasury/subscription-receiver")
    attachments.update({"receiver-" + name: vault / name for name in ["keystore.json", "passphrase", "public.json"]})
    payload, report = seal(databases, key, attachments)
    filename = "runtime-" + report["ciphertext_sha256"][:16] + ".encrypted"
    encrypted = PRIVATE / filename
    if not encrypted.exists():
        fd = os.open(encrypted, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    restored = PRIVATE / (filename + ".restore")
    proof = restore(encrypted.read_bytes(), key, restored)
    result = report | {"restore_verified": proof["restore_verified"], "restored_files": len(proof["files"]),
        "encrypted_path": str(encrypted), "off_host_verified": False,
        "disaster_recovery_key_custody": "SEPARATE_EXTERNAL_KEY_CUSTODY_REQUIRED", "transactions_submitted_by_rehearsal": 0,
        "snapshot_assurance": "SQLITE_CONSISTENT_PER_DATABASE_NOT_CROSS_DATABASE_ATOMIC"}
    if args.off_host_config:
        from machine_commerce.offsite import export_and_restore
        result.update(export_and_restore(encrypted, key, args.off_host_config))
    (ROOT / "artifacts/production-paths/backup-rehearsal.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - private errors must not expose file contents.
        raise SystemExit("Backup rehearsal failed: " + type(exc).__name__) from None
