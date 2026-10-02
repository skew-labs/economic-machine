"""One-use, browser-bound SIWE login for EOA owners; never payment authority."""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime
from urllib.parse import urlsplit

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys.exceptions import BadSignature
from eth_utils import to_checksum_address
from eth_utils.exceptions import ValidationError

from economic_machine.values import MachineError, require_keys

CHAINS = frozenset({1, 42161, 421614})
CHALLENGE_TTL = 300
SESSION_TTL = 86400


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


class WalletAuth:
    def __init__(self, store, origin):
        self.store, self.origin = store, origin
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS wallet_challenges (
                id TEXT PRIMARY KEY, address TEXT NOT NULL, chain_id INTEGER NOT NULL,
                binding_hash TEXT NOT NULL, message TEXT NOT NULL, expires INTEGER NOT NULL,
                used_at INTEGER)""")
            db.execute("""CREATE TABLE IF NOT EXISTS wallet_identities (
                address TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL REFERENCES sessions(id),
                chain_id INTEGER NOT NULL)""")

    def challenge(self, raw):
        require_keys(raw, {"address", "chain_id"}, "wallet challenge")
        if not self.origin or urlsplit(self.origin).scheme != "https":
            raise MachineError("wallet login requires a configured HTTPS origin")
        if not isinstance(raw["address"], str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", raw["address"]):
            raise MachineError("valid Ethereum wallet address required")
        if type(raw["chain_id"]) is not int or raw["chain_id"] not in CHAINS:
            raise MachineError("use Ethereum, Arbitrum One or Arbitrum Sepolia for sign-in")
        address, chain = to_checksum_address(raw["address"]), raw["chain_id"]
        now, expires = self.store.clock(), self.store.clock() + CHALLENGE_TTL
        cid, binding, nonce = secrets.token_hex(16), secrets.token_urlsafe(32), secrets.token_hex(16)
        message = (
            f"{urlsplit(self.origin).netloc} wants you to sign in with your Ethereum account:\n"
            f"{address}\n\nSign in to skew Economic Machine. This does not authorize payments.\n\n"
            f"URI: {self.origin}/commerce/console\nVersion: 1\nChain ID: {chain}\nNonce: {nonce}\n"
            f"Issued At: {timestamp(now)}\nExpiration Time: {timestamp(expires)}\nRequest ID: {cid}")
        with self.store.connect() as db:
            db.execute("DELETE FROM wallet_challenges WHERE expires<=?", (now,))
            db.execute("INSERT INTO wallet_challenges VALUES (?,?,?,?,?,?,NULL)",
                       (cid, address, chain, hashed(binding), message, expires))
        return {"challenge_id": cid, "message": message, "expires_at": expires}, binding

    def verify(self, raw, binding):
        require_keys(raw, {"challenge_id", "signature"}, "wallet sign-in")
        cid, signature = raw["challenge_id"], raw["signature"]
        if (not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{32}", cid)
                or not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature)
                or not isinstance(binding, str) or not 20 <= len(binding) <= 120):
            raise PermissionError("invalid or expired wallet sign-in")
        # Exclusive transaction couples signature consumption and session rotation.
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM wallet_challenges WHERE id=?", (cid,)).fetchone()
            now = self.store.clock()
            if (not row or row["used_at"] is not None or row["expires"] <= now
                    or not hmac.compare_digest(row["binding_hash"], hashed(binding))):
                raise PermissionError("invalid or expired wallet sign-in")
            try:
                recovered = Account.recover_message(encode_defunct(text=row["message"]), signature=signature)
            except (ValueError, TypeError, OverflowError, BadSignature, ValidationError) as exc:
                raise PermissionError("wallet signature could not be verified") from exc
            if recovered.lower() != row["address"].lower():
                raise PermissionError("wallet signature could not be verified")
            owner = db.execute("SELECT session_id FROM wallet_identities WHERE address=?",
                               (row["address"].lower(),)).fetchone()
            token = secrets.token_urlsafe(32)
            if owner:
                sid = owner["session_id"]
                db.execute("UPDATE sessions SET token_hash=?,expires=? WHERE id=?",
                           (hashed(token), now + SESSION_TTL, sid))
                db.execute("UPDATE wallet_identities SET chain_id=? WHERE session_id=?", (row["chain_id"], sid))
            else:
                sid = "buyer-" + secrets.token_hex(12)
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,0,0,0,0)",
                           (sid, hashed(token), now, now + SESSION_TTL))
                db.execute("INSERT INTO wallet_identities VALUES (?,?,?)",
                           (row["address"].lower(), sid, row["chain_id"]))
            db.execute("UPDATE wallet_challenges SET used_at=? WHERE id=?", (now, cid))
            self.store._event(db, "wallet-login:" + cid, "WALLET_AUTHENTICATED",
                              {"owner": sid, "at": now, "chain_id": row["chain_id"]})
        return sid, token

    def identity(self, sid):
        with self.store.connect() as db:
            row = db.execute("SELECT address,chain_id FROM wallet_identities WHERE session_id=?", (sid,)).fetchone()
            return {"address": to_checksum_address(row["address"]), "chain_id": row["chain_id"],
                    "method": "siwe"} if row else None

    def logout(self, sid):
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET token_hash=? WHERE id=?", (hashed(secrets.token_urlsafe(32)), sid))
            self.store._event(db, "logout:" + secrets.token_hex(16), "OWNER_SIGNED_OUT",
                              {"owner": sid, "at": self.store.clock()})
        # Agent keys and their limits survive owner sign-out. No payment is authorized here.
