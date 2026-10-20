"""Public login configuration and owner-scoped CLI observation; no signing API."""

import json
import os
import re
import stat
import time
from pathlib import Path

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
APP_ID = re.compile(r"[a-zA-Z0-9_-]{8,100}\Z")


def public_wallet_config():
    app_id = os.environ.get("MACHINE_PRIVY_APP_ID", "")
    client = os.environ.get("MACHINE_PRIVY_CLIENT_ID", "")
    if not APP_ID.fullmatch(app_id) or (client and not APP_ID.fullmatch(client)):
        return {"privy": None}
    return {"privy": {"app_id": app_id, "client_id": client or None}}


def wallet_csp(*, privy=False):
    if not privy:
        return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    # Privy's styled modal and isolated signer iframe require these exceptions.
    # No unsafe script execution, wildcard script host, or general HTTPS egress.
    return ("default-src 'self'; script-src 'self' https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
            "frame-src https://auth.privy.io https://verify.walletconnect.com "
            "https://verify.walletconnect.org https://challenges.cloudflare.com; "
            "connect-src 'self' https://auth.privy.io https://*.rpc.privy.systems "
            "https://arb1.arbitrum.io https://sepolia-rollup.arbitrum.io "
            "wss://relay.walletconnect.com wss://relay.walletconnect.org wss://www.walletlink.org "
            "https://explorer-api.walletconnect.com; worker-src 'self'; manifest-src 'self'")


def privy_asset(web, asset):
    # Build outputs only; no source maps, parent directories, config, or secrets.
    if not re.fullmatch(r"(?:entry|chunk-[A-Z0-9]+)\.js(?:\.LEGAL\.txt)?", asset):
        return None
    target = Path(web) / "privy" / asset
    return target if target.is_file() and not target.is_symlink() else None


def agent_wallet_status(identity, now=None):
    owner = os.environ.get("MACHINE_METAMASK_OWNER_ADDRESS", "")
    if (not ADDRESS.fullmatch(owner) or not identity
            or identity.get("address", "").lower() != owner.lower()):
        return {"status": "NOT_CONFIGURED"}
    path = os.environ.get("MACHINE_METAMASK_STATUS_FILE", "")
    if not path:
        return {"status": "NOT_CONFIGURED"}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192 or info.st_mode & 0o022:
                raise ValueError("invalid snapshot")
            raw = json.loads(handle.read(8193))
        observed = raw["observed_at"]
        if type(observed) is not int or not 0 <= int(now if now is not None else time.time()) - observed <= 180:
            return {"status": "STALE"}
        address = raw.get("address", "")
        if (raw.get("authenticated") is not True or raw.get("initialized") is not True
                or raw.get("mode") != "guard" or not isinstance(address, str)
                or not ADDRESS.fullmatch(address) or int(address, 16) == 0):
            return {"status": "LOGIN_REQUIRED"}
        return {"status": "READY", "address": address, "mode": "guard", "observed_at": observed,
                "network": "eip155:42161", "capabilities": ["READ_BALANCES"],
                "signing_authority": "EXTERNAL_METAMASK_APPROVAL"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "UNAVAILABLE"}
