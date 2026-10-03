"""Remote owner-local disaster recovery; keys and salts never enter stdout."""

import argparse
import json
import os
from pathlib import Path

from machine_commerce.offsite import export_and_restore
from machine_engine.solution_recovery import private_file, recover, snapshot


def main():
    if not str(Path(__file__).resolve()).startswith("/srv/skew/"):
        raise SystemExit("Trusted remote host required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("snapshot")
    create.add_argument("--directory", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--off-host-config")
    restore = sub.add_parser("restore")
    restore.add_argument("--input", required=True)
    restore.add_argument("--destination", required=True)
    args = parser.parse_args()
    key = private_file(args.key_file, 32).read_bytes()
    if len(key) != 32:
        raise ValueError("32 byte key required")
    if args.command == "snapshot":
        payload, report = snapshot(args.directory, key)
        target = Path(args.output)
        if not target.is_absolute() or target.parent.is_symlink() or target.parent.stat().st_mode & 0o077:
            raise ValueError("Private absolute output required")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if args.off_host_config:
            report.update(export_and_restore(target, key, args.off_host_config))
    else:
        report = recover(private_file(args.input, 256 * 1024 * 1024).read_bytes(), key, args.destination)
    print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - never log private salt/key/database content.
        raise SystemExit("Recovery failed: " + type(error).__name__) from None
