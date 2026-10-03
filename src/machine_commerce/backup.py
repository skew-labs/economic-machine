"""Consistent SQLite snapshots in an authenticated encrypted backup envelope.

Keys never enter an archive or public report. Restores use a fresh directory,
verify the whole authentication tag before writing, and validate every journal.
Off-host transfer and separate disaster-recovery key custody are explicit gates.
"""

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from Crypto.Cipher import AES

from economic_machine.journal import verify_journal
from economic_machine.values import MachineError, canonical

MAGIC = b"MACHINE-BACKUP-1\n"
MAX_BYTES = 256 * 1024 * 1024


def inspect_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MachineError("BACKUP_DATABASE_INTEGRITY_FAILED")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise MachineError("BACKUP_FOREIGN_KEY_CHECK_FAILED")
        if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'").fetchone() and not verify_journal(db):
            raise MachineError("BACKUP_JOURNAL_INTEGRITY_FAILED")
        return {"integrity": "ok", "journal_valid": True}
    finally:
        db.close()


def seal(databases, key, attachments=None):
    if not isinstance(key, bytes) or len(key) != 32 or not 1 <= len(databases) <= 256:
        raise MachineError("BACKUP_KEY_AND_BOUNDED_DATABASES_REQUIRED")
    archive, manifest = io.BytesIO(), []
    with tempfile.TemporaryDirectory() as temp, zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for index, source in enumerate(databases):
            source = Path(source)
            if source.is_symlink() or not source.is_file():
                raise MachineError("EXISTING_BACKUP_DATABASE_REQUIRED")
            snapshot = Path(temp) / f"database-{index}.sqlite3"
            origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
            output = sqlite3.connect(snapshot)
            try:
                origin.backup(output, pages=128)
            finally:
                output.close()
                origin.close()
            inspect_database(snapshot)
            content = snapshot.read_bytes()
            if len(content) > MAX_BYTES or archive.tell() + len(content) > MAX_BYTES:
                raise MachineError("BACKUP_SIZE_LIMIT")
            manifest.append({"name": snapshot.name, "kind": "sqlite", "size": len(content),
                             "source_filename": source.name, "source_directory": source.parent.name,
                             "sha256": hashlib.sha256(content).hexdigest()})
            bundle.writestr(snapshot.name, content)
        for name, source in (attachments or {}).items():
            if (not isinstance(name, str) or "/" in name or "\\" in name or name in {".", "..", "manifest.json", "restore-manifest.json"}
                    or name.startswith("database-") or any(item["name"] == name for item in manifest)):
                raise MachineError("EXACT_ATTACHMENT_NAME_REQUIRED")
            source = Path(source)
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
                raise MachineError("BOUNDED_PRIVATE_ATTACHMENT_REQUIRED")
            content = source.read_bytes()
            manifest.append({"name": name, "kind": "private-file", "size": len(content),
                             "sha256": hashlib.sha256(content).hexdigest()})
            bundle.writestr(name, content)
        if len(manifest) > 300:
            raise MachineError("BACKUP_ENTRY_LIMIT")
        bundle.writestr("manifest.json", canonical({"schema": 1, "files": manifest}))
    plaintext = archive.getvalue()
    if len(plaintext) > MAX_BYTES:
        raise MachineError("BACKUP_SIZE_LIMIT")
    cipher = AES.new(key, AES.MODE_GCM, nonce=os.urandom(12))
    cipher.update(MAGIC)
    encrypted, tag = cipher.encrypt_and_digest(plaintext)
    payload = MAGIC + cipher.nonce + tag + encrypted
    return payload, {"ciphertext_sha256": hashlib.sha256(payload).hexdigest(), "ciphertext_bytes": len(payload),
        "database_count": len(databases), "private_file_count": len(manifest) - len(databases), "algorithm": "AES-256-GCM",
        "off_host_verified": False, "restore_verified": False, "key_in_archive": False}


def restore(payload, key, destination):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink() or destination.parent.is_symlink():
        raise MachineError("NEW_RESTORE_DIRECTORY_REQUIRED")
    if len(payload) > MAX_BYTES + len(MAGIC) + 28 or not payload.startswith(MAGIC) or len(key) != 32:
        raise MachineError("INVALID_BACKUP_ENVELOPE")
    offset = len(MAGIC)
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=payload[offset:offset + 12])
        cipher.update(MAGIC)
        content = cipher.decrypt_and_verify(payload[offset + 28:], payload[offset + 12:offset + 28])
    except ValueError:
        raise MachineError("BACKUP_AUTHENTICATION_FAILED") from None
    verified = []
    with zipfile.ZipFile(io.BytesIO(content)) as bundle, tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        entries = bundle.infolist()
        if (not 2 <= len(entries) <= 301 or len({p.filename for p in entries}) != len(entries)
                or any(p.compress_type != zipfile.ZIP_STORED for p in entries)
                or sum(p.file_size for p in entries) > MAX_BYTES):
            raise MachineError("INVALID_BACKUP_ARCHIVE")
        manifest = json.loads(bundle.read("manifest.json"))
        if (manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list)
                or {p.filename for p in entries} != {p["name"] for p in manifest["files"]} | {"manifest.json"}):
            raise MachineError("BACKUP_MANIFEST_MISMATCH")
        for item in manifest["files"]:
            name = item["name"]
            if "/" in name or "\\" in name or name in {".", ".."}:
                raise MachineError("BACKUP_PATH_TRAVERSAL")
            data = bundle.read(name)
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise MachineError("BACKUP_CONTENT_MISMATCH")
            path = Path(temp) / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if item["kind"] == "sqlite":
                inspect_database(path)
            elif item["kind"] != "private-file":
                raise MachineError("INVALID_BACKUP_ENTRY_KIND")
            verified.append({"name": name, "sha256": item["sha256"], "verified": True})
        # Do not overwrite a live directory, even on a competing restore.
        destination.mkdir(mode=0o700, exist_ok=False)
        for item in verified:
            os.rename(Path(temp) / item["name"], destination / item["name"])
        fd = os.open(destination / 'restore-manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(canonical(manifest)); output.flush(); os.fsync(output.fileno())
    return {"restore_verified": True, "files": verified, "target_overwritten": False,
            "signatures_generated": 0, "transactions_submitted": 0}
