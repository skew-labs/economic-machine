"""One explicitly approved sale on public Arbitrum Sepolia, not a general signer.

Run separately from the buyer runtime. Only the test seller's gas key is loaded;
the buyer's EIP-3009 signature arrives via x402. Persist the computed transaction
hash BEFORE the one allowed broadcast. Recovery reads that hash, never resends.
"""

import argparse
import base64
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_typed_data, hash_domain
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from web3 import Web3
from web3.exceptions import TransactionNotFound

from economic_machine.values import MachineError, canonical, digest, require_keys
from machine_commerce.payments import typed_authorization
from machine_commerce.transport import AUTH_USED, TRANSFER, topic_address
from machine_commerce.x402 import PaymentBinding, address, decode_header

CHAIN_ID = 421614
USDC = "0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d"
RPC = "https://sepolia-rollup.arbitrum.io/rpc"
MAX_AMOUNT = 10_000  # 0.01 valueless Circle test USDC, never customer money.
MAX_GAS_WEI = 300_000_000_000_000
ABI = [
    {"type": "function", "name": name, "stateMutability": "view", "inputs": inputs,
     "outputs": [{"type": output}]}
    for name, inputs, output in [
        ("decimals", [], "uint8"), ("DOMAIN_SEPARATOR", [], "bytes32"),
        ("balanceOf", [{"name": "account", "type": "address"}], "uint256"),
        ("authorizationState", [{"name": "authorizer", "type": "address"},
                                {"name": "nonce", "type": "bytes32"}], "bool")]
] + [{"type": "function", "name": "transferWithAuthorization", "stateMutability": "nonpayable",
      "inputs": [{"name": name, "type": kind} for name, kind in [
          ("from", "address"), ("to", "address"), ("value", "uint256"),
          ("validAfter", "uint256"), ("validBefore", "uint256"), ("nonce", "bytes32"),
          ("v", "uint8"), ("r", "bytes32"), ("s", "bytes32")]], "outputs": []}]


def header(body):
    return base64.b64encode(canonical(body)).decode()


def hexbytes(value):
    return "0x" + bytes(value).hex()


def validate_sale(sale):
    require_keys(sale, {"profile", "payment_id", "payer", "amount_atoms", "request", "data"}, "approved test sale")
    profile = sale["profile"]
    if (profile["network"] != "eip155:421614" or address(profile["asset"]) != USDC
            or profile["rpc_url"] != RPC or profile["token_name"] != "USD Coin"
            or profile["token_version"] != "2" or profile["max_timeout_seconds"] != 120
            or profile["finality"] != "finalized"):
        raise MachineError("only approved Circle USDC on Arbitrum Sepolia is supported")
    if type(sale["amount_atoms"]) is not int or not 0 < sale["amount_atoms"] <= MAX_AMOUNT:
        raise MachineError("test payment exceeds the fixed 0.01 USDC cap")
    address(sale["payer"])
    address(profile["pay_to"])
    request = sale["request"]
    require_keys(request, {"terms_hash", "data_version", "units", "purpose", "license"}, "approved sale request")
    if (not re.fullmatch(r"[0-9a-f]{64}", request["terms_hash"])
            or request["data_version"] != profile["data_version"]
            or request["units"] != 1 or request["purpose"] != "research"
            or request["license"] != "internal-use"):
        raise MachineError("test sale terms differ from the approved snapshot")
    binding = PaymentBinding(request["terms_hash"], profile["url"], profile["network"],
                             profile["asset"], profile["pay_to"], sale["amount_atoms"], 1,
                             120, "USD Coin", "2")
    binding.validate()
    return binding


class SepoliaChain:
    """Fixed-network seller gas payer. Never an engine signing capability."""

    def __init__(self, key_file):
        material = json.loads(Path(key_file).read_text())
        self.account = Account.from_key(material["private_key"])
        if address(self.account.address) != address(material["address"]):
            raise MachineError("test seller key does not match its approved address")
        self.w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 8}))
        self.token = self.w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)

    def verify_network(self, binding):
        if self.w3.eth.chain_id != CHAIN_ID or address(self.account.address) != address(binding.pay_to):
            raise MachineError("test seller chain or recipient mismatch")
        domain = {"name": binding.token_name, "version": binding.token_version,
                  "chainId": CHAIN_ID, "verifyingContract": binding.asset}
        if (self.token.functions.decimals().call() != 6
                or self.token.functions.DOMAIN_SEPARATOR().call() != hash_domain(domain)):
            raise MachineError("live test token domain mismatch")

    def signed_transaction(self, binding, authorization, signature):
        self.verify_network(binding)
        payer = Web3.to_checksum_address(authorization["from"])
        nonce = bytes.fromhex(authorization["nonce"][2:])
        if self.token.functions.authorizationState(payer, nonce).call():
            raise MachineError("authorization was already used; no replacement payment")
        if self.token.functions.balanceOf(payer).call() < binding.amount_atoms:
            raise MachineError("test buyer USDC balance is insufficient")
        signature = bytes.fromhex(signature[2:])
        call = self.token.functions.transferWithAuthorization(
            payer, Web3.to_checksum_address(binding.pay_to), binding.amount_atoms,
            int(authorization["validAfter"]), int(authorization["validBefore"]), nonce,
            signature[64], signature[:32], signature[32:64])
        sender = self.account.address
        call.call({"from": sender})  # Actual token simulation before signing gas.
        pending = self.w3.eth.get_transaction_count(sender, "pending")
        if pending != self.w3.eth.get_transaction_count(sender, "latest"):
            raise MachineError("seller has a pending transaction; no competing nonce")
        gas = (call.estimate_gas({"from": sender}) * 12 + 9) // 10
        price = self.w3.eth.gas_price * 2
        if gas * price > MAX_GAS_WEI or self.w3.eth.get_balance(sender) < gas * price:
            raise MachineError("test seller needs gas within the fixed ETH cap")
        transaction = call.build_transaction({"from": sender, "nonce": pending, "chainId": CHAIN_ID,
                                             "gas": gas, "gasPrice": price})
        signed = self.account.sign_transaction(transaction)
        return hexbytes(signed.hash), signed.raw_transaction

    def broadcast(self, raw):
        return hexbytes(self.w3.eth.send_raw_transaction(raw))

    def receipt(self, tx_hash):
        try:
            return self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None


class Merchant:
    def __init__(self, sale, chain, db_path, clock=time.time):
        self.sale, self.chain, self.clock = sale, chain, clock
        self.binding = validate_sale(sale)
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sales (id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, "
                       "tx_hash TEXT NOT NULL, nonce TEXT NOT NULL, status TEXT NOT NULL, broadcasts INTEGER NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def requirement(self):
        b = self.binding
        return {"scheme": "exact", "network": b.network, "asset": address(b.asset),
                "payTo": address(b.pay_to), "amount": str(b.amount_atoms), "maxTimeoutSeconds": 120,
                "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"}}

    def validate_payment(self, encoded):
        payment = decode_header(encoded)
        require_keys(payment, {"x402Version", "resource", "accepted", "payload"}, "x402 signature")
        if (type(payment["x402Version"]) is not int or payment["x402Version"] != 2
                or payment["resource"] != {"url": self.binding.resource_url}
                or canonical(payment["accepted"]) != canonical(self.requirement())):
            raise MachineError("payment does not match the one approved sale")
        payload = payment["payload"]
        require_keys(payload, {"signature", "authorization"}, "EIP-3009 payload")
        auth = payload["authorization"]
        require_keys(auth, {"from", "to", "value", "validAfter", "validBefore", "nonce"}, "authorization")
        for key in ["value", "validAfter", "validBefore"]:
            if not isinstance(auth[key], str) or re.fullmatch(r"0|[1-9][0-9]{0,77}", auth[key]) is None:
                raise MachineError("canonical authorization integers required")
        if (address(auth["from"]) != address(self.sale["payer"])
                or address(auth["to"]) != address(self.binding.pay_to)
                or auth["value"] != str(self.binding.amount_atoms)
                or not isinstance(auth["nonce"], str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", auth["nonce"])):
            raise MachineError("payer, recipient, amount or nonce differs from approved test sale")
        signature = payload["signature"]
        if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
            raise MachineError("bounded EOA signature required")
        recovered = Account.recover_message(encode_typed_data(full_message=typed_authorization(self.binding, auth)),
                                            signature=signature)
        if address(recovered) != address(self.sale["payer"]):
            raise MachineError("test buyer signature mismatch")
        return payment, auth, signature

    def paid(self, receipt, row):
        if not receipt or receipt["status"] != 1 or hexbytes(receipt["transactionHash"]) != row["tx_hash"]:
            return False
        used, transfers = [], []
        for event in receipt["logs"]:
            if address(event["address"]) != USDC or event.get("removed", False):
                continue
            topics = [hexbytes(t).lower() for t in event["topics"]]
            if topics == [AUTH_USED, topic_address(self.sale["payer"]), row["nonce"].lower()]:
                used.append(event)
            if topics == [TRANSFER, topic_address(self.sale["payer"]), topic_address(self.binding.pay_to)]:
                transfers.append(int.from_bytes(event["data"], "big"))
        return len(used) == 1 and transfers == [self.binding.amount_atoms]

    def handle(self, body, headers):
        if canonical(body) != canonical(self.sale["request"]) or headers.get("idempotency-key") != self.sale["payment_id"]:
            raise MachineError("unregistered test order")
        encoded = headers.get("payment-signature")
        if not encoded:
            return 402, {"PAYMENT-REQUIRED": header({"x402Version": 2,
                "resource": {"url": self.binding.resource_url}, "accepts": [self.requirement()]})}, {}
        payment, auth, signature = self.validate_payment(encoded)
        intent_hash = digest({"body": body, "payment": payment})
        raw = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sales WHERE id=?", (self.sale["payment_id"],)).fetchone()
            if row:
                if row["input_hash"] != intent_hash:
                    raise MachineError("signed order changed; never create a replacement transfer")
            else:
                now, after, before = int(self.clock()), int(auth["validAfter"]), int(auth["validBefore"])
                if not after < now < before or before - after > 130 or before > now + 120:
                    raise MachineError("authorization outside the bounded test window")
                tx_hash, raw = self.chain.signed_transaction(self.binding, auth, signature)
                db.execute("INSERT INTO sales VALUES (?,?,?,?,'BROADCAST_COMMITTED',1)", (
                    self.sale["payment_id"], intent_hash, tx_hash, auth["nonce"]))
            row = dict(db.execute("SELECT * FROM sales WHERE id=?", (self.sale["payment_id"],)).fetchone())
        # Transaction hash is now durable. A crash or timeout never signs/broadcasts again.
        if raw is not None:
            try:
                if self.chain.broadcast(raw) != row["tx_hash"]:
                    raise MachineError("RPC returned an unexpected transaction hash")
            except Exception:  # noqa: BLE001 - every ambiguous send retains its hash; never log bearer signatures.
                return self.pending(row)
        for _ in range(8):
            receipt = self.chain.receipt(row["tx_hash"])
            if self.paid(receipt, row):
                with self.connect() as db:
                    db.execute("UPDATE sales SET status='RECEIPT_CONFIRMED' WHERE id=?", (row["id"],))
                result = {"success": True, "network": self.binding.network, "payer": address(self.sale["payer"]),
                          "transaction": row["tx_hash"], "amount": str(self.binding.amount_atoms)}
                return 200, {"PAYMENT-RESPONSE": header(result)}, {**body, "data": self.sale["data"]}
            time.sleep(0.25)
        return self.pending(row)

    def pending(self, row):
        return 202, {"PAYMENT-RESPONSE": header({"success": False, "errorReason": "settlement_pending",
            "network": self.binding.network, "payer": address(self.sale["payer"]),
            "transaction": row["tx_hash"]})}, {"status": "PENDING", "safe_to_retry_payment": False}


def create_app(merchant):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/commerce-sepolia/data")
    async def data(request: Request):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 50_000:
                return JSONResponse({"error": "bounded JSON request required"}, status_code=413)
            chunks.append(chunk)
        try:
            body = json.loads(b"".join(chunks))
            # Blocking RPC/SQLite work stays in a worker thread.
            from starlette.concurrency import run_in_threadpool
            status, headers, delivered = await run_in_threadpool(merchant.handle, body, request.headers)
            # Buyer runtime expects exactly these delivery keys, never signature plaintext.
            if status == 200:
                delivered = {key: delivered[key] for key in ("terms_hash", "data_version", "data")}
            return JSONResponse(delivered, status_code=status, headers={**headers, "Cache-Control": "no-store"})
        except (MachineError, ValueError, TypeError, KeyError):
            return JSONResponse({"error": "test sale admission rejected"}, status_code=409)
        except Exception:  # noqa: BLE001 - sanitize every RPC/signing failure before the public HTTP boundary.
            return JSONResponse({"error": "test seller observation unavailable; reconcile existing order"}, status_code=503)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=4262)
    args = parser.parse_args()
    sale = json.loads(Path(args.config).read_text())
    chain = SepoliaChain(args.key_file)
    chain.verify_network(validate_sale(sale))
    import uvicorn
    uvicorn.run(create_app(Merchant(sale, chain, args.db)), host="127.0.0.1", port=args.port,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
