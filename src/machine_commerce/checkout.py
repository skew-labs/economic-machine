"""Buyer checkout over the existing bilateral market and noncustodial payments.

Catalog availability is computed from registered, current seller rules. Checkout
never seeds inventory, signs, submits or converts sandbox credits to tokens.
Prepaid subscriptions grant a bounded entitlement only after finalized payment
and a terms-bound delivery; renewal always requires another customer approval.
"""

import json
import secrets

from economic_machine.values import MachineError, canonical, digest, require_keys

from .domain import bounded_int, identifier, money_atoms, money_string
from .market import normalize_demand
from .x402 import address

SCHEMA = """
CREATE TABLE IF NOT EXISTS commerce_checkouts (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id),
 idempotency_key TEXT NOT NULL, input_hash TEXT NOT NULL, body TEXT NOT NULL,
 demand_id TEXT NOT NULL, match_id TEXT NOT NULL UNIQUE, terms_hash TEXT NOT NULL,
 payment_id TEXT UNIQUE, mandate_id TEXT, created INTEGER NOT NULL,
 UNIQUE(owner,idempotency_key));
CREATE TABLE IF NOT EXISTS commerce_entitlements (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id),
 checkout_id TEXT NOT NULL UNIQUE REFERENCES commerce_checkouts(id),
 plan_id TEXT NOT NULL, plan_hash TEXT NOT NULL, resource_id TEXT NOT NULL,
 payment_id TEXT NOT NULL UNIQUE, starts INTEGER NOT NULL, expires INTEGER NOT NULL);
"""

DEFAULT_PLANS = {"atlas-monthly": {
    "name": "Atlas Monthly", "description": "A prepaid month of Atlas data access for your agents.",
    "resource_id": "atlas-monthly", "duration_seconds": 30 * 86400, "price": "10"}}
USDC_ASSETS = {"eip155:42161": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
               "eip155:421614": "0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d"}


def validate_plans(raw, profiles):
    if not isinstance(raw, dict) or len(raw) > 50:
        raise MachineError("bounded subscription plan registry required")
    plans, resources = {}, set()
    for pid, plan in raw.items():
        identifier(pid, "subscription plan ID")
        require_keys(plan, {"name", "description", "resource_id", "duration_seconds", "price"}, "subscription plan")
        rid = plan["resource_id"]
        identifier(rid, "subscription resource ID")
        if (rid in resources or (rid in profiles and profiles[rid]["data_type"] != "subscription." + pid)):
            raise MachineError("subscription needs a unique registered SKU resource")
        if rid in profiles and address(profiles[rid]["asset"]) != USDC_ASSETS.get(profiles[rid]["network"]):
            raise MachineError("subscription must settle in Circle USDC on the approved Arbitrum network")
        for field, limit in [("name", 80), ("description", 240)]:
            if (not isinstance(plan[field], str) or not 1 <= len(plan[field]) <= limit
                    or any(ord(c) < 32 for c in plan[field])):
                raise MachineError("printable subscription description required")
        bounded_int(plan["duration_seconds"], 3600, 366 * 86400, "subscription period")
        if money_atoms(plan["price"]) <= 0:
            raise MachineError("positive approved subscription price required")
        plans[pid] = {**plan, "id": pid, "price": money_string(money_atoms(plan["price"])),
                      "billing": "PREPAID_MANUAL_RENEWAL", "auto_charge": False}
        resources.add(rid)
    return plans


class Checkout:
    def __init__(self, store, market, payments, plans=None):
        self.store, self.market, self.payments = store, market, payments
        self.plans = validate_plans(plans or {}, payments.profiles)
        with store.connect() as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    def _offers(self, db, rid, profile, now):
        asset = profile["network"] + "/erc20:" + address(profile["asset"])
        rows = db.execute("SELECT s.* FROM supplies s JOIN sessions u ON u.id=s.owner "
            "WHERE s.owner=? AND s.data_type=? AND s.expires>? AND u.expires>? "
            "ORDER BY s.id LIMIT 100", (profile["seller_owner"], profile["data_type"], now, now))
        result = []
        for row in rows:
            body = json.loads(row["body"])
            if (body["version"] != profile["data_version"] or body["payment_asset"] != asset
                    or not -5 <= now - body["updated_at"] < 86400):
                continue
            result.append({"supply_id": row["id"], "resource_id": rid, "name": body["name"],
                "unit_price": money_string(body["ask_atoms"]), "minimum_price": money_string(body["floor_atoms"]),
                "min_units": body["min_units"], "max_units": body["max_units"],
                "purposes": body["purposes"], "licenses": body["licenses"],
                "updated_at": body["updated_at"], "expires": row["expires"],
                "refresh_seconds": body["refresh_seconds"], "response_seconds": body["response_seconds"]})
        return result

    def catalog(self):
        now = self.store.clock()
        products = []
        with self.store.connect() as db:
            for rid, profile in self.payments.profiles.items():
                kind = ("subscription" if profile["data_type"].startswith("subscription.") else
                        "compute" if profile["data_type"].startswith("compute.") else "data")
                offers = self._offers(db, rid, profile, now)
                products.append({"id": rid, "category": kind, "data_type": profile["data_type"],
                    "version": profile["data_version"], "network": profile["network"],
                    "asset": address(profile["asset"]), "pay_to": address(profile["pay_to"]),
                    "token_name": profile["token_name"], "decimals": 6, "offers": offers,
                    "status": "AVAILABLE" if offers else "NO_ACTIVE_SELLER_RULE",
                    "settlement": "X402_EIP3009", "capacity_verified": False})
        plans = []
        for plan in self.plans.values():
            product = next((p for p in products if p["id"] == plan["resource_id"]), None)
            plans.append({**plan, "plan_hash": digest(plan), "product": product,
                          "currency": "USDC", "status": product["status"] if product else "PAYMENT_PROVIDER_NOT_CONFIGURED"})
        return {"as_of": now, "products": products, "plans": plans,
                "engine": {"name": "Engine Community", "price": "0", "license": "MIT", "self_hosted": True},
                "custody": "NONE", "signing_authority": "CUSTOMER_EXTERNAL",
                "subscription_billing": "PREPAID_MANUAL_RENEWAL", "auto_charge": False}

    def quote(self, sid, raw):
        require_keys(raw, {"resource_id", "supply_id", "plan_id", "units", "purpose", "license",
            "max_total", "max_age_seconds", "max_refresh_seconds", "response_seconds", "idempotency_key"}, "checkout")
        for key in ["resource_id", "supply_id", "idempotency_key"]:
            identifier(raw[key], key)
        profile = self.payments.profiles.get(raw["resource_id"])
        if not profile:
            raise MachineError("approved payment resource required")
        plan = None
        if raw["plan_id"] is not None:
            identifier(raw["plan_id"], "plan ID")
            plan = self.plans.get(raw["plan_id"])
            if not plan or plan["resource_id"] != raw["resource_id"] or raw["units"] != 1:
                raise MachineError("approved one-period subscription required")
        elif profile["data_type"].startswith("subscription."):
            raise MachineError("choose the approved subscription plan")
        total = money_atoms(raw["max_total"])
        normalized = normalize_demand({"data_type": profile["data_type"], "payment_asset":
            profile["network"] + "/erc20:" + address(profile["asset"]), "purpose": raw["purpose"],
            "license": raw["license"], "units": raw["units"], "max_unit_price": money_string(total),
            "max_total_price": raw["max_total"], "max_age_seconds": raw["max_age_seconds"],
            "max_refresh_seconds": raw["max_refresh_seconds"], "response_seconds": raw["response_seconds"],
            "ttl_seconds": 600})
        now, input_hash = self.store.clock(), digest(raw)
        with self.store.connect() as db:
            self.market._active(db, sid, now)
            prior = db.execute("SELECT * FROM commerce_checkouts WHERE owner=? AND idempotency_key=?",
                               (sid, raw["idempotency_key"])).fetchone()
            if prior:
                if prior["input_hash"] != input_hash:
                    raise MachineError("checkout idempotency key reused with changed conditions")
                return self._public(prior)
            offers = self._offers(db, raw["resource_id"], profile, now)
            if not any(o["supply_id"] == raw["supply_id"] for o in offers):
                raise MachineError("seller offer expired or is outside the approved resource")
            if db.execute("SELECT COUNT(*) FROM demands WHERE data_type=? AND expires>?",
                          (profile["data_type"], now)).fetchone()[0] >= 100:
                raise MachineError("active demand admission limit reached")
            did = "demand-" + secrets.token_hex(12)
            db.execute("INSERT INTO demands VALUES (?,?,?,?,?)", (did, sid, normalized["data_type"],
                canonical(normalized).decode(), now + 600))
            self.store._event(db, did, "DEMAND_REGISTERED", {"record_id": did, "owner": sid,
                "policy_hash": digest(normalized), "at": now})
            self.market._match(db, normalized["data_type"], now, demand_id=did, supply_id=raw["supply_id"])
            matches = self.market._demand_matches(db, did, now)
            if not matches:
                raise MachineError("seller conditions do not meet your budget, freshness or usage rights")
            match = matches[0]
            if plan and money_atoms(match["terms"]["total_price"]) != money_atoms(plan["price"]):
                raise MachineError("seller quote differs from approved subscription price")
            cid = "checkout-" + secrets.token_hex(12)
            body = {"resource_id": raw["resource_id"], "profile_hash": digest(profile), "plan": plan,
                    "plan_hash": digest(plan) if plan else None, "agreement": match}
            db.execute("INSERT INTO commerce_checkouts VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?)",
                (cid, sid, raw["idempotency_key"], input_hash, canonical(body).decode(), did,
                 match["id"], match["terms_hash"], now))
            self.store._event(db, cid + ":quoted", "COMMERCE_CHECKOUT_QUOTED", {
                "checkout_id": cid, "terms_hash": match["terms_hash"], "plan_hash": body["plan_hash"]})
            return self._public(db.execute("SELECT * FROM commerce_checkouts WHERE id=?", (cid,)).fetchone())

    def _row(self, db, sid, cid):
        row = db.execute("SELECT * FROM commerce_checkouts WHERE id=? AND owner=?", (cid, sid)).fetchone()
        if not row:
            raise MachineError("owned checkout required")
        return row

    @staticmethod
    def _public(row):
        body = json.loads(row["body"])
        return {"id": row["id"], "resource_id": body["resource_id"], "created": row["created"],
                "agreement": body["agreement"], "payment_id": row["payment_id"], "plan": body["plan"],
                "plan_hash": body["plan_hash"], "signing_authority": "CUSTOMER_EXTERNAL", "auto_charge": False}

    def prepare(self, sid, cid, mandate_id):
        identifier(mandate_id, "payment mandate ID")
        with self.store.connect() as db:
            row = self._row(db, sid, cid)
            body = json.loads(row["body"])
            if digest(self.payments.profiles.get(body["resource_id"])) != body["profile_hash"]:
                raise MachineError("resource registry changed; refresh the purchase quote")
            if row["mandate_id"] and row["mandate_id"] != mandate_id:
                raise MachineError("checkout is already bound to another payment limit")
            # Persist intent before the separately transactional payment reservation.
            # A crash retries the exact same request and payment idempotency key.
            db.execute("UPDATE commerce_checkouts SET mandate_id=? WHERE id=?", (mandate_id, cid))
        try:
            payment = self.payments.prepare(sid, {"match_id": row["match_id"], "terms_hash": row["terms_hash"],
                "mandate_id": mandate_id, "resource_id": body["resource_id"], "idempotency_key": cid})
        except MachineError:
            with self.store.connect() as db:
                if not db.execute("SELECT id FROM payments WHERE owner=? AND idempotency_key=?", (sid, cid)).fetchone():
                    db.execute("UPDATE commerce_checkouts SET mandate_id=NULL WHERE id=? AND mandate_id=?",
                               (cid, mandate_id))
            raise
        with self.store.connect() as db:
            db.execute("UPDATE commerce_checkouts SET payment_id=? WHERE id=? AND owner=?", (payment["id"], cid, sid))
        return self.get(sid, cid)

    def get(self, sid, cid):
        with self.store.connect() as db:
            row = self._row(db, sid, cid)
            if not row["payment_id"] and row["mandate_id"]:
                recovered = db.execute("SELECT * FROM payments WHERE owner=? AND idempotency_key=?",
                                       (sid, cid)).fetchone()
                if recovered:
                    if recovered["match_id"] != row["match_id"] or recovered["mandate_id"] != row["mandate_id"]:
                        raise MachineError("checkout recovery payment identity mismatch")
                    db.execute("UPDATE commerce_checkouts SET payment_id=? WHERE id=?", (recovered["id"], cid))
                    self.store._event(db, cid + ":payment-linked", "COMMERCE_CHECKOUT_PAYMENT_RECOVERED", {
                        "checkout_id": cid, "payment_id": recovered["id"]})
                    row = self._row(db, sid, cid)
            result = self._public(row)
            if not row["payment_id"]:
                return {**result, "status": "QUOTED", "payment": None, "entitlement": None}
            # Use the same transaction for the payment read and one-time entitlement.
            payment_row = self.payments._row(db, sid, row["payment_id"])
            payment = self.payments._public(payment_row)
            body, now = json.loads(row["body"]), self.store.clock()
            entitlement_error = None
            if body["plan"] and payment["status"] == "SETTLED":
                observation = payment["observation"] or {}
                artifact = (payment["delivery"] or {}).get("artifact", {})
                delivered = artifact.get("data") if isinstance(artifact, dict) else None
                grant = delivered.get("subscription_grant") if isinstance(delivered, dict) else None
                expected = {"plan_id": body["plan"]["id"], "plan_hash": body["plan_hash"],
                            "payer": payment["payer"], "duration_seconds": body["plan"]["duration_seconds"]}
                if (observation.get("status") != "PAID" or observation.get("finality") != "finalized"
                        or grant != expected):
                    entitlement_error = "SUBSCRIPTION_DELIVERY_NOT_VERIFIED"
                existing = db.execute("SELECT id FROM commerce_entitlements WHERE checkout_id=?", (cid,)).fetchone()
                if not existing and not entitlement_error:
                    eid = "entitlement-" + secrets.token_hex(12)
                    # An early, manually approved renewal adds a period instead
                    # of wasting the buyer's remaining paid access.
                    tail = db.execute("SELECT MAX(expires) FROM commerce_entitlements WHERE owner=? AND plan_id=?",
                                      (sid, body["plan"]["id"])).fetchone()[0]
                    starts = max(now, tail or now)
                    db.execute("INSERT INTO commerce_entitlements VALUES (?,?,?,?,?,?,?,?,?)", (
                        eid, sid, cid, body["plan"]["id"], body["plan_hash"], body["resource_id"],
                        payment["id"], starts, starts + body["plan"]["duration_seconds"]))
                    self.store._event(db, eid, "SUBSCRIPTION_PERIOD_ACTIVATED", {
                        "entitlement_id": eid, "checkout_id": cid, "payment_id": payment["id"],
                        "plan_hash": body["plan_hash"], "starts": starts,
                        "expires": starts + body["plan"]["duration_seconds"], "auto_charge": False})
            grant = db.execute("SELECT * FROM commerce_entitlements WHERE checkout_id=?", (cid,)).fetchone()
            entitlement = dict(grant) if grant else None
            if entitlement:
                entitlement["status"] = ("EXPIRED" if entitlement["expires"] <= now else
                                         "SCHEDULED" if entitlement["starts"] > now else "ACTIVE")
            return {**result, "status": payment["status"], "payment": payment, "entitlement": entitlement,
                    "entitlement_error": entitlement_error}

    def snapshot(self, sid):
        with self.store.connect() as db:
            self.market._active(db, sid, self.store.clock())
            ids = [r[0] for r in db.execute("SELECT id FROM commerce_checkouts WHERE owner=? "
                                           "ORDER BY rowid DESC LIMIT 100", (sid,))]
        return {"checkouts": [self.get(sid, cid) for cid in ids], "auto_charge": False}

    def require_access(self, sid, plan_id):
        identifier(plan_id, "subscription plan ID")
        self.snapshot(sid)
        now = self.store.clock()
        with self.store.connect() as db:
            grant = db.execute("SELECT * FROM commerce_entitlements WHERE owner=? AND plan_id=? "
                "AND starts<=? AND expires>? ORDER BY expires DESC LIMIT 1", (sid, plan_id, now, now)).fetchone()
            if not grant:
                raise MachineError("active paid subscription access required")
            return dict(grant)
