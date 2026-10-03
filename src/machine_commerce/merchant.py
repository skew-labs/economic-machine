"""Hosted subscription seller using an external exact-EVM facilitator.

No wallet key is held here. Only an existing checkout's customer authorization
can be forwarded. An ambiguous settle call is never repeated. Delivery can be
recovered from finalized chain evidence without sending another signature.
"""

import base64
import hashlib
import json
import secrets

from economic_machine.values import MachineError, canonical, digest, require_keys

from .payments import validate_signature
from .transport import HTTPS, https_url
from .x402 import PaymentBinding, address


def encoded(value):
    return base64.b64encode(canonical(value)).decode()


class HostedMerchant:
    def __init__(self, checkout, config=None, transport=None):
        self.checkout, self.payments = checkout, checkout.payments
        self.store = checkout.store
        self.config = config or {}
        urls = set()
        if not isinstance(self.config, dict) or len(self.config) > 20:
            raise MachineError("bounded hosted merchant configuration required")
        for rid, entry in self.config.items():
            require_keys(entry, {"facilitator_url"}, "hosted merchant")
            https_url(entry["facilitator_url"])
            if entry["facilitator_url"].endswith("/"):
                raise MachineError("facilitator base URL must omit its trailing slash")
            plan = next((p for p in checkout.plans.values() if p["resource_id"] == rid), None)
            profile = self.payments.profiles.get(rid)
            if (not plan or not profile or profile["data_type"] != "subscription." + plan["id"]
                    or not profile["url"].endswith("/api/commerce/merchant/" + rid)):
                raise MachineError("hosted merchant requires a matching subscription resource and route")
            urls.update(entry["facilitator_url"] + "/" + path for path in ["verify", "settle"])
        self.transport = transport or HTTPS(urls)
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS merchant_attempts (payment_id TEXT PRIMARY KEY "
                       "REFERENCES payments(id), intent_hash TEXT NOT NULL, status TEXT NOT NULL, "
                       "tx_hash TEXT, created INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS merchant_publishers (owner TEXT PRIMARY KEY "
                       "REFERENCES sessions(id), resource_id TEXT UNIQUE NOT NULL)")
            if "last_checked" not in {r[1] for r in db.execute("PRAGMA table_info(merchant_attempts)")}:
                db.execute("ALTER TABLE merchant_attempts ADD COLUMN last_checked INTEGER NOT NULL DEFAULT 0")

    def refresh_offers(self):
        """Publish immutable offers for explicitly configured managed sellers.

        Human operator sessions are never extended here. Old offers and agreed
        timestamps remain immutable so an in-flight checkout remains valid.
        """
        from .market import normalize_supply
        from .domain import money_atoms
        now = self.store.clock()
        published = 0
        for rid in self.config:
            profile = self.payments.profiles[rid]
            owner = profile["seller_owner"]
            if owner != "merchant-" + rid:
                continue
            plan = next(p for p in self.checkout.plans.values() if p["resource_id"] == rid)
            with self.store.connect() as db:
                registered = db.execute("SELECT resource_id FROM merchant_publishers WHERE owner=?", (owner,)).fetchone()
                existing = db.execute("SELECT id FROM sessions WHERE id=?", (owner,)).fetchone()
                if existing and (not registered or registered[0] != rid):
                    raise MachineError("managed merchant identity conflicts with an existing account")
                if not existing:
                    # No usable credential is issued or stored for this internal publisher.
                    token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                    db.execute("INSERT INTO sessions VALUES (?,?,?,?,0,0,0,0)", (owner, token_hash, now, now + 86400))
                    db.execute("INSERT INTO merchant_publishers VALUES (?,?)", (owner, rid))
                db.execute("UPDATE sessions SET expires=? WHERE id=?", (now + 86400, owner))
                current = db.execute("SELECT body FROM supplies WHERE owner=? AND expires>? ORDER BY rowid DESC LIMIT 1",
                                     (owner, now + 900)).fetchone()
                if current:
                    body = json.loads(current[0])
                    if (body["version"] == profile["data_version"] and body["ask_atoms"] == money_atoms(plan["price"])
                            and body["payment_asset"] == profile["network"] + "/erc20:" + address(profile["asset"])):
                        continue
                raw = {"name": plan["name"], "data_type": profile["data_type"], "version": profile["data_version"],
                    "payment_asset": profile["network"] + "/erc20:" + address(profile["asset"]),
                    "unit_price": plan["price"], "floor_price": plan["price"], "discount_bps": 0,
                    "discount_min_units": 1, "min_units": 1, "max_units": 1, "purposes": ["research", "automation"],
                    "licenses": ["internal-use"], "updated_at": now, "refresh_seconds": 3600,
                    "response_seconds": 30, "ttl_seconds": 3600}
                body = normalize_supply(raw, now)
                supply_id = "supply-" + secrets.token_hex(12)
                db.execute("INSERT INTO supplies VALUES (?,?,?,?,?)", (supply_id, owner, body["data_type"],
                    canonical(body).decode(), now + 3600))
                self.store._event(db, supply_id, "MANAGED_SUBSCRIPTION_OFFER_PUBLISHED", {
                    "resource_id": rid, "supply_id": supply_id, "policy_hash": digest(body), "at": now})
                published += 1
        return published

    def order(self, rid, pid, request):
        if rid not in self.config:
            raise MachineError("hosted subscription seller is not configured")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
            order = db.execute("SELECT * FROM commerce_checkouts WHERE payment_id=?", (pid,)).fetchone()
            if not row or not order:
                raise MachineError("existing subscription checkout required")
            body, purchase = json.loads(row["body"]), json.loads(order["body"])
            self.payments._profile(body)
            plan = purchase["plan"]
            if (body["resource_id"] != rid or not plan or plan["resource_id"] != rid
                    or purchase["plan_hash"] != digest(plan)
                    or canonical(request) != canonical(body["request"])
                    or row["owner"] != order["owner"]):
                raise MachineError("merchant request differs from its bound checkout")
            if row["status"] in {"CANCELLED", "EXPIRED_UNPAID"}:
                raise MachineError("checkout is no longer payable")
            return dict(row), body, purchase

    @staticmethod
    def requirement(body):
        b = PaymentBinding(**body["binding"])
        return {"scheme": "exact", "network": b.network, "asset": b.asset,
                "payTo": b.pay_to, "amount": str(b.amount_atoms),
                "maxTimeoutSeconds": b.max_timeout_seconds,
                "extra": {"name": b.token_name, "version": b.token_version}}

    def handle(self, rid, pid, request, signature=None):
        row, body, purchase = self.order(rid, pid, request)
        requirement = self.requirement(body)
        if not signature:
            if (row["status"] not in {"PREPARED", "CHALLENGE_READY"}
                    or body["binding"]["expires"] <= self.store.clock()):
                raise MachineError("active unsigned checkout required")
            return 402, {"PAYMENT-REQUIRED": encoded({"x402Version": 2,
                "resource": {"url": body["binding"]["resource_url"]}, "accepts": [requirement]})}, {}
        if row["status"] not in {"SUBMITTED", "UNKNOWN", "SETTLEMENT_REPORTED", "SETTLED", "PAID_DELIVERY_MISSING"}:
            raise MachineError("customer must submit through the budget-bound checkout")
        # An old signed replay may recover a settled result after expiry, but
        # only with the exact payload already admitted by the buyer boundary.
        from .x402 import decode_header
        payload = decode_header(signature)
        if not row["signature_hash"] or digest(payload) != row["signature_hash"]:
            raise MachineError("merchant signature differs from the admitted customer authorization")
        intent = digest({"request": request, "payload": payload})
        with self.store.connect() as db:
            prior = db.execute("SELECT * FROM merchant_attempts WHERE payment_id=?", (pid,)).fetchone()
            if prior and prior["intent_hash"] != intent:
                raise MachineError("merchant idempotency conflict")
        if not prior:
            validate_signature(signature, body, self.store.clock())
            call = {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirement}
            base = self.config[rid]["facilitator_url"]
            status, _, response = self.transport.call(base + "/verify", call)
            verified = json.loads(response)
            if (status != 200 or verified.get("isValid") is not True
                    or address(verified.get("payer")) != address(body["authorization"]["from"])):
                raise MachineError("facilitator did not verify the exact customer authorization")
            with self.store.connect() as db:
                # The transaction arbitrates concurrent callers. Commit before
                # the only possible external settlement transmission.
                current = db.execute("SELECT * FROM merchant_attempts WHERE payment_id=?", (pid,)).fetchone()
                if current and current["intent_hash"] != intent:
                    raise MachineError("merchant idempotency conflict")
                first = current is None
                if first:
                    db.execute("INSERT INTO merchant_attempts (payment_id,intent_hash,status,tx_hash,created) "
                               "VALUES (?,?,'ATTEMPT_COMMITTED',NULL,?)",
                               (pid, intent, self.store.clock()))
                    self.store._event(db, pid + ":merchant-attempt", "MERCHANT_SETTLEMENT_ATTEMPT_COMMITTED",
                                      {"payment_id": pid, "intent_hash": intent})
            if first:
                try:
                    status, _, response = self.transport.call(base + "/settle", call)
                    from .x402 import inspect_response
                    result = inspect_response(encoded(json.loads(response)), PaymentBinding(**body["binding"]))
                    with self.store.connect() as db:
                        db.execute("UPDATE merchant_attempts SET status='REPORTED',tx_hash=? WHERE payment_id=?",
                                   (result["tx_hash"], pid))
                except Exception:
                    # The facilitator may already have broadcast. Recover by
                    # nonce on the chain; never retry or log bearer signatures.
                    pass
        return self.deliver(rid, pid)

    def deliver(self, rid, pid):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
            attempt = db.execute("SELECT * FROM merchant_attempts WHERE payment_id=?", (pid,)).fetchone()
            if not row or not attempt:
                raise MachineError("existing admitted merchant attempt required")
            body = json.loads(row["body"])
            if rid != body["resource_id"] or rid not in self.config:
                raise MachineError("merchant resource mismatch")
            profile = self.payments._profile(body)
            if "start_block" not in body:
                raise MachineError("admitted challenge observation required")
        try:
            observed = self.payments.chain.observe(profile, body["authorization"],
                attempt["tx_hash"], body["start_block"])
        except Exception:
            observed = {"status": "PENDING"}
        if observed.get("status") != "PAID" or observed.get("finality") != "finalized":
            return 202, {}, {"status": "SETTLEMENT_PENDING", "safe_to_retry_payment": False}
        # Paid readback alone is insufficient if the returned identity changed.
        from .transport import hash32
        tx = hash32(observed.get("tx_hash"))
        if attempt["tx_hash"] and tx != attempt["tx_hash"]:
            raise MachineError("finalized merchant transaction differs from its recorded settlement")
        with self.store.connect() as db:
            order = db.execute("SELECT * FROM commerce_checkouts WHERE payment_id=?", (pid,)).fetchone()
            purchase = json.loads(order["body"])
            plan = purchase["plan"]
            artifact = {"terms_hash": body["binding"]["terms_hash"], "data_version": body["request"]["data_version"],
                "data": {"subscription_grant": {"plan_id": plan["id"], "plan_hash": purchase["plan_hash"],
                    "payer": body["authorization"]["from"], "duration_seconds": plan["duration_seconds"]}}}
            delivery = {"assurance": "TERMS_AND_VERSION_BINDING_NOT_DATA_TRUTH", "artifact": artifact,
                        "artifact_hash": digest(artifact)}
            # Do not release the budget here. The existing buyer reconciliation
            # owns the sole reserved -> spent transition.
            db.execute("UPDATE payments SET delivery=?,status=CASE WHEN status='PAID_DELIVERY_MISSING' "
                       "THEN 'SETTLED' ELSE status END WHERE id=? AND status IN "
                       "('SUBMITTED','UNKNOWN','SETTLEMENT_REPORTED','PAID_DELIVERY_MISSING','SETTLED')",
                       (canonical(delivery).decode(), pid))
            db.execute("UPDATE merchant_attempts SET status='DELIVERED',tx_hash=? WHERE payment_id=?", (tx, pid))
            self.store._event(db, pid + ":merchant-delivered", "MERCHANT_SUBSCRIPTION_GRANT_DELIVERED",
                              {"payment_id": pid, "tx_hash": tx, "artifact_hash": digest(artifact)})
        return 200, {"PAYMENT-RESPONSE": encoded({"success": True, "network": profile["network"],
            "transaction": tx, "payer": body["authorization"]["from"], "amount": str(body["binding"]["amount_atoms"])})}, artifact

    def recover(self):
        with self.store.connect() as db:
            pending = [(r["payment_id"], json.loads(r["body"])["resource_id"]) for r in db.execute(
                "SELECT m.payment_id,p.body FROM merchant_attempts m JOIN payments p ON p.id=m.payment_id "
                "WHERE m.status!='DELIVERED' AND p.status NOT IN ('CANCELLED','EXPIRED_UNPAID') "
                "ORDER BY m.last_checked,m.created LIMIT 10")]
            for pid, _ in pending:
                db.execute("UPDATE merchant_attempts SET last_checked=? WHERE payment_id=?", (self.store.clock(), pid))
        failures = delivered = 0
        for pid, rid in pending:
            try:
                delivered += self.deliver(rid, pid)[0] == 200
            except Exception:
                failures += 1
        return {"scanned": len(pending), "delivered": delivered, "failures": failures}
