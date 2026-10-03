"""Encrypted salt + WAL recovery. Restored miners remain held, never auto-resume."""

import ctypes
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak

from economic_machine.values import MachineError
from machine_commerce.backup import restore, seal

from .solution_client import reveal
from .solution_operations import private_directory

MAX_SECRETS = 160000  # protocol bound: 10,000 rounds * 16 problems


def publish_noreplace(source, destination):
    # Linux atomic directory publication without replacing a concurrently
    # created empty directory. No unsafe rename fallback on unsupported hosts.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise MachineError("SOLUTION_ATOMIC_RECOVERY_PUBLICATION_REQUIRED")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise MachineError("SOLUTION_RECOVERY_DESTINATION_NOT_PUBLISHED")


def private_file(path, limit):
    path = Path(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.geteuid()
        or path.stat().st_mode & 0o077
        or path.stat().st_nlink != 1
        or path.stat().st_size > limit
    ):
        raise MachineError("SOLUTION_PRIVATE_RECOVERY_FILE")
    return path


def validate_snapshot(database, directory):
    db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise MachineError("SOLUTION_RECOVERY_DATABASE")
        count = 0
        for job, binding, result, payload in db.execute("""SELECT j.id,j.binding,j.result,i.payload
                FROM intents i JOIN jobs j ON i.job=j.id WHERE i.action='commit' """):
            if re.fullmatch("[a-f0-9]{64}", job) is None:
                raise MachineError("SOLUTION_RECOVERY_JOB_ID")
            path = private_file(Path(directory) / (job + ".secret.json"), 8192)
            reveal(path)  # independent commitment recomputation; never logged
            secret, bound, answer, intent = (
                json.loads(path.read_bytes()),
                json.loads(binding),
                json.loads(result),
                json.loads(payload),
            )
            expected = {
                "contract": bound["contract"],
                "chain": bound["chain"],
                "round": str(bound["round"]),
                "problem": bound["problem"],
                "miner": bound["miner"],
                "bits": answer["bits"],
                "graph_sha256": answer["graph_sha256"],
            }
            if any(secret.get(k) != v for k, v in expected.items()):
                raise MachineError("SOLUTION_RECOVERY_SECRET_BINDING")
            data = keccak(text="commit(uint256,uint8,bytes32)")[:4] + encode(
                ["uint256", "uint8", "bytes32"],
                [bound["round"], bound["problem"], bytes.fromhex(secret["commitment"])],
            )
            tx = intent["transaction"]
            if (
                tx.get("data") != "0x" + data.hex()
                or tx.get("to") != bound["contract"]
                or tx.get("from") != bound["miner"]
                or tx.get("chainId") != bound["chain"]
                or tx.get("value") != "0x0"
            ):
                raise MachineError("SOLUTION_RECOVERY_INTENT_BINDING")
            count += 1
            if count > MAX_SECRETS:
                raise MachineError("SOLUTION_RECOVERY_ENTRY_BOUND")
        return {
            "verified_commit_secrets": count,
            "jobs": db.execute("SELECT count(*) FROM jobs").fetchone()[0],
        }
    finally:
        db.close()


def snapshot(directory, key):
    directory = private_directory(directory)
    database = private_file(directory / "miner.sqlite", 128 * 1024 * 1024)
    lock = sqlite3.connect(database, isolation_level=None, timeout=5)
    try:
        # Freeze journal writes while the read connection snapshots WAL and all
        # already-persisted commitments' secrets. Concurrent unfinished secrets
        # cause validation failure rather than a false recovery acceptance.
        lock.execute("BEGIN IMMEDIATE")
        files = sorted(directory.glob("*.secret.json"))
        if len(files) > MAX_SECRETS:
            raise MachineError("SOLUTION_RECOVERY_ENTRY_BOUND")
        proof = validate_snapshot(database, directory)
        # Pack secrets into the *snapshot* database, not hundreds of archive
        # entries and not the live journal. This supports a long-lived miner
        # while retaining the outer authenticated archive's small entry bound.
        with tempfile.TemporaryDirectory() as temp:
            packed = Path(temp) / "miner.sqlite"
            origin = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            output = sqlite3.connect(packed)
            try:
                origin.backup(output)
                output.execute("DROP TABLE IF EXISTS recovery_secrets")
                output.execute("CREATE TABLE recovery_secrets(name TEXT PRIMARY KEY, content BLOB NOT NULL)")
                secret_bytes = 0
                for path in files:
                    if re.fullmatch(r"[a-f0-9]{64}\.secret\.json", path.name) is None:
                        raise MachineError("SOLUTION_RECOVERY_SECRET_NAME")
                    private_file(path, 8192)
                    reveal(path)
                    content = path.read_bytes()
                    secret_bytes += len(content)
                    if secret_bytes > 128 * 1024 * 1024:
                        raise MachineError("SOLUTION_RECOVERY_SIZE_BOUND")
                    output.execute("INSERT INTO recovery_secrets VALUES(?,?)", (path.name, content))
                output.commit()
            finally:
                output.close()
                origin.close()
            packed.chmod(0o600)
            if packed.stat().st_size > 128 * 1024 * 1024:
                raise MachineError("SOLUTION_RECOVERY_SIZE_BOUND")
            payload, report = seal([packed], key)
        return payload, report | proof | {
            "snapshot_scope": "ONE_MINER_JOURNAL_AND_COMMIT_SECRETS",
            "automatic_resume": False,
            "key_in_archive": False,
        }
    finally:
        if lock.in_transaction:
            lock.execute("ROLLBACK")
        lock.close()


def recover(payload, key, destination):
    destination = Path(destination)
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise MachineError("SOLUTION_NEW_RECOVERY_DESTINATION")
    private_directory(destination.parent)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        staging = Path(temp) / "verified"
        restore(payload, key, staging)
        (staging / "database-0.sqlite3").rename(staging / "miner.sqlite")
        source = sqlite3.connect(staging / "miner.sqlite")
        try:
            for count, (name, content) in enumerate(
                source.execute("SELECT name,content FROM recovery_secrets"), start=1
            ):
                if (
                    count > MAX_SECRETS
                    or re.fullmatch(r"[a-f0-9]{64}\.secret\.json", name) is None
                    or not isinstance(content, bytes)
                    or len(content) > 8192
                ):
                    raise MachineError("SOLUTION_RECOVERY_SECRET_PACK")
                descriptor = os.open(
                    staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(descriptor, "wb") as secret:
                    secret.write(content)
                    secret.flush()
                    os.fsync(secret.fileno())
                reveal(staging / name)
        finally:
            source.close()
        proof = validate_snapshot(staging / "miner.sqlite", staging)
        hold = {
            "schema": "solution-recovery-hold-1",
            "ciphertext_sha256": hashlib.sha256(payload).hexdigest(),
            "reason": "OLD_HOST_FENCING_LATER_BUDGET_AND_TRANSACTION_RECONCILIATION_REQUIRED",
            "automatic_resume": False,
        }
        descriptor = os.open(staging / "RECOVERY_HOLD", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(hold, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        db = sqlite3.connect(staging / "miner.sqlite")
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS recovery_control(id INTEGER PRIMARY KEY CHECK(id=1), held INTEGER NOT NULL)"
            )
            db.execute("INSERT OR REPLACE INTO recovery_control VALUES(1,1)")
            db.execute("UPDATE intents SET state='HELD'")
            db.commit()
        finally:
            db.close()
        # Publish only the fully authenticated, validated, held state.
        publish_noreplace(staging, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return proof | {
        "restore_verified": True,
        "recovery_hold": True,
        "automatic_signing": False,
        "transactions_submitted": 0,
        "off_host_verified": False,
    }
