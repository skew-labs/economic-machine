"""Remote HTTPS login probe with a disposable EOA. No keys or payment authority issued."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

ORIGIN = "https://machine.148-113-153-116.nip.io"
PREFIX = "/commerce/api"


def main():
    wallet = Account.create()  # Memory only. Never funded, saved, printed or used for settlement.
    checks = {}
    with httpx.Client(base_url=ORIGIN, timeout=20, headers={"Origin": ORIGIN}) as client:
        response = client.post(PREFIX + "/auth/challenge", json={"address": wallet.address, "chain_id": 421614})
        assert response.status_code == 200
        challenge = response.json()
        assert f"URI: {ORIGIN}/commerce/console" in challenge["message"]
        signature = wallet.sign_message(encode_defunct(text=challenge["message"]))
        proof = {"challenge_id": challenge["challenge_id"], "signature": "0x" + signature.signature.hex()}
        response = client.post(PREFIX + "/auth/verify", json=proof)
        assert response.status_code == 200
        assert response.json()["identity"]["address"] == wallet.address
        assert response.json()["snapshot"]["balance"] == "0"
        cookie = response.headers["set-cookie"].lower()
        assert all(s in cookie for s in ["secure", "httponly", "samesite=strict", "path=/commerce/"])
        checks["https_wallet_signature_verified"] = True
        checks["secure_scoped_owner_cookie"] = True
        sid = response.json()["snapshot"]["buyer_id"]
        for path in ["/workspace", "/keys", "/payments", "/auth/session"]:
            assert client.get(PREFIX + path).status_code == 200
        checks["wallet_owner_management_reads"] = True
        assert client.post(PREFIX + "/sessions", json={}).json()["resumed"] is True
        assert client.post(PREFIX + "/auth/verify", json=proof).status_code == 401
        checks["nonce_replay_rejected"] = True
        assert client.post(PREFIX + "/auth/logout", json={}).status_code == 200
        assert client.get(PREFIX + "/workspace").status_code == 401
        checks["sign_out_revokes_cookie"] = True
        challenge = client.post(PREFIX + "/auth/challenge", json={"address": wallet.address, "chain_id": 1}).json()
        signature = wallet.sign_message(encode_defunct(text=challenge["message"]))
        response = client.post(PREFIX + "/auth/verify", json={"challenge_id": challenge["challenge_id"],
                              "signature": "0x" + signature.signature.hex()})
        assert response.status_code == 200
        assert response.json()["snapshot"]["buyer_id"] == sid
        assert response.json()["snapshot"]["balance"] == "0"
        checks["relogin_preserves_zero_capital_identity"] = True
        assert client.post(PREFIX + "/auth/logout", json={}).status_code == 200
    result = {"checked_at": datetime.now(UTC).isoformat(), "checks": checks,
              "scope": "Disposable EOA HTTPS auth only. Customer extension signatures not exercised.",
              "payments": 0, "transaction_broadcasts": 0, "agent_keys_created": 0}
    target = Path(__file__).resolve().parents[1] / "artifacts/wallet-design/https-login.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
