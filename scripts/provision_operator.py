"""Run on the trusted host: write a password hash without printing credentials."""

import argparse
import getpass
import json
import os
import tempfile
from pathlib import Path

from machine_commerce.operations import password_hash, valid_password_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    if not 1 <= len(args.username) <= 60:
        parser.error("username must be 1 to 60 characters")
    values = json.loads(args.file.read_text()) if args.file.exists() else {}
    if not isinstance(values, dict) or any(not valid_password_hash(v) for v in values.values()):
        parser.error("existing operator registry is invalid")
    password = getpass.getpass("New operator password: ")
    if not 12 <= len(password) <= 256 or password != getpass.getpass("Confirm password: "):
        parser.error("matching passwords of 12 to 256 characters required")
    if args.username not in values and len(values) >= 100:
        parser.error("operator admission limit reached")
    values[args.username] = password_hash(password)
    args.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=args.file.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(values, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, args.file)
        directory = os.open(args.file.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Operator password hash saved. No password or API key was printed.")


if __name__ == "__main__":
    main()
