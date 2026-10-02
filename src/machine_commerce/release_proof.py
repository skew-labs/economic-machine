"""Tamper-evident file manifest; no claim that hashes certify source truth."""

import hashlib
import json
from pathlib import Path

from economic_machine.values import MachineError, digest, require_keys

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "artifacts/atlas-release/manifest.json"


def safe_path(root, relative):
    if not isinstance(relative, str) or len(relative) > 300:
        raise MachineError("BOUNDED_PROOF_PATH_REQUIRED")
    parts = Path(relative)
    if parts.is_absolute() or ".." in parts.parts or not parts.parts:
        raise MachineError("PROOF_PATH_ESCAPE")
    current = Path(root)
    for part in parts.parts:
        current = current / part
        if current.is_symlink():
            raise MachineError("PROOF_SYMLINK_REJECTED")
    if not current.is_file() or not current.resolve().is_relative_to(Path(root).resolve()):
        raise MachineError("PROOF_FILE_REQUIRED")
    return current


def file_evidence(root, relative):
    path = safe_path(root, relative)
    if path.stat().st_size > 20_000_000:
        raise MachineError("PROOF_FILE_SIZE_LIMIT")
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return {"path": relative, "bytes": path.stat().st_size, "sha256": checksum.hexdigest()}


def verify_manifest(raw, root=ROOT):
    require_keys(raw, {"schema", "created_at", "files", "claims", "assurance", "manifest_sha256"}, "release proof")
    if (raw["schema"] != "machine-release-proof-1" or not isinstance(raw["files"], list)
            or not 1 <= len(raw["files"]) <= 500 or type(raw["created_at"]) is not int):
        raise MachineError("PROOF_MANIFEST_SCHEMA")
    body = {k: v for k, v in raw.items() if k != "manifest_sha256"}
    if digest(body) != raw["manifest_sha256"]:
        raise MachineError("PROOF_MANIFEST_HASH_MISMATCH")
    seen = set()
    for entry in raw["files"]:
        require_keys(entry, {"path", "bytes", "sha256"}, "proof file")
        if entry["path"] in seen or entry != file_evidence(root, entry["path"]):
            raise MachineError("PROOF_FILE_HASH_MISMATCH")
        seen.add(entry["path"])
    return {"accepted": True, "manifest_sha256": raw["manifest_sha256"], "files_checked": len(seen),
            "assurance": "FILE_INTEGRITY_NOT_EXTERNAL_TRUTH_OR_PRODUCTION_CERTIFICATION"}


def public_manifest():
    path = safe_path(ROOT, str(MANIFEST.relative_to(ROOT)))
    if path.stat().st_size > 200000:
        raise MachineError("PROOF_MANIFEST_SIZE_LIMIT")
    raw = json.loads(path.read_text())
    return {"manifest": raw, "verification": verify_manifest(raw)}
