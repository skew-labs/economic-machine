"""Negative evidence checks using copies of an actually completed study."""

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from audit_qwen_comparison import audit


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def corrupt_billing(directory):
    path = directory / "result.json"
    result = json.loads(path.read_text())
    result["all_actual_provider_usage"]["total_tokens"] += 1
    write(path, result)


def corrupt_choice(directory):
    result = json.loads((directory / "result.json").read_text())
    for row in result["records"]:
        if row["arm"] == "machine" and row["eligible"]:
            row["choice_id"] = "seller-0-1"  # incompatible license in the frozen book
            break
    write(directory / "result.json", result)
    (directory / "trades.jsonl").write_text("".join(json.dumps(row) + "\n" for row in result["records"]))


def corrupt_balances(directory):
    result = json.loads((directory / "result.json").read_text())
    result["test_token_balances"]["recipient_atoms"] -= 1
    write(directory / "result.json", result)


def corrupt_source(directory):
    protocol = json.loads((directory / "protocol.json").read_text())
    path = directory / "source_snapshot" / next(iter(protocol["sources"]))
    path.write_bytes(path.read_bytes() + b"\n# changed after commitment\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    audit(args.directory)
    outcomes = []
    for corruption in [corrupt_billing, corrupt_choice, corrupt_balances, corrupt_source]:
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "study"
            shutil.copytree(args.directory, copy, ignore=shutil.ignore_patterns("*.sqlite3", "*.sqlite3-*"))
            corruption(copy)
            try:
                audit(copy)
            except AssertionError:
                outcomes.append({"case": corruption.__name__, "rejected": True})
            else:
                raise RuntimeError("audit accepted corrupted evidence: " + corruption.__name__)
    print(json.dumps({"checks": outcomes, "passed": len(outcomes), "original_evidence_unchanged": True}, indent=2))


if __name__ == "__main__":
    main()
