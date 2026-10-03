"""Create an encrypted receiving wallet on the trusted server, never a signer.

The API service receives only the public address. The root-only keystore and
passphrase stay outside the repository and outside its service credentials.
This protects against API-service access, not compromise of the server root.
"""

import fcntl
import json
import os
import secrets
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from eth_account import Account

VAULT = Path("/var/lib/skew-treasury/subscription-receiver")
FILES = {"keystore.json", "passphrase", "public.json"}


def secure_path(path, mode, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("INSECURE_VAULT_PATH")
    if not directory and info.st_nlink != 1:
        raise ValueError("LINKED_VAULT_FILE")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_secret(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def validate_vault(vault):
    secure_path(vault, 0o700, directory=True)
    if {p.name for p in vault.iterdir()} != FILES:
        raise ValueError("INCOMPLETE_VAULT")
    for name in FILES:
        secure_path(vault / name, 0o600)
    public = json.loads((vault / "public.json").read_bytes())
    keystore = json.loads((vault / "keystore.json").read_bytes())
    password = (vault / "passphrase").read_bytes()
    if len(password) != 64 or public.get("network") != "eip155:42161" or public.get("purpose") != "subscription-receiver":
        raise ValueError("INVALID_VAULT_METADATA")
    if keystore.get("crypto", {}).get("kdf") != "scrypt" or keystore["crypto"]["kdfparams"].get("n", 0) < 262144:
        raise ValueError("WEAK_KEYSTORE")
    key = Account.decrypt(keystore, password)
    if Account.from_key(key).address != public.get("address"):
        raise ValueError("KEY_ADDRESS_MISMATCH")
    del key, password
    return public


def provision(vault):
    vault = Path(vault).absolute()
    # Refuse symlink ancestors before creating or opening any secret files.
    if any(p.is_symlink() for p in [vault, *vault.parents]):
        raise ValueError("SYMLINK_VAULT_PATH")
    vault.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    secure_path(vault.parent, 0o700, directory=True)
    lock = vault.parent / ".provision.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        secure_path(lock, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        if vault.exists():
            return validate_vault(vault), False
        stage = Path(tempfile.mkdtemp(prefix=".receiver-", dir=vault.parent))
        try:
            account = Account.create()
            password = secrets.token_hex(32).encode("ascii")
            encrypted = Account.encrypt(account.key, password, kdf="scrypt", iterations=262144)
            public = {"address": account.address, "network": "eip155:42161", "purpose": "subscription-receiver",
                "created_at": datetime.now(UTC).isoformat()}
            write_secret(stage / "keystore.json", json.dumps(encrypted).encode())
            write_secret(stage / "passphrase", password)
            write_secret(stage / "public.json", json.dumps(public).encode())
            del account, password, encrypted
            validate_vault(stage)
            sync_directory(stage)
            os.rename(stage, vault)
            sync_directory(vault.parent)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return public, True
    finally:
        os.close(fd)


def main():
    if os.geteuid() != 0 or not str(Path(__file__).resolve()).startswith("/srv/skew/"):
        raise SystemExit("Root operation on the trusted remote host required")
    try:
        public, created = provision(VAULT)
        print(json.dumps({**public, "created": created, "encrypted_keystore": True,
            "key_address_verified": True, "vault_directory_mode": "0700", "vault_file_modes": "0600",
            "api_service_key_access": False, "payments_sent": 0, "transactions_signed": 0,
            "off_host_backup": False}))
    except Exception as exc:
        # Decryption errors and filesystem paths must not expose secret material.
        raise SystemExit("Wallet provisioning failed: " + type(exc).__name__) from None


if __name__ == "__main__":
    main()
