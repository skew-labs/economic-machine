"""Event-driven bilateral matching and bounded negotiation.

The runtime connects policies; it does not own, endorse, or guarantee the data.
No LLM, wallet signer, or payment facilitator is invoked here.
"""

import json
import re
import secrets

from economic_machine.values import MachineError, canonical, digest, require_keys

from .domain import bounded_int, money_atoms, money_string
from .x402 import address

PURPOSES = {"research", "commercial", "automation"}
LICENSES = {"internal-use", "commercial-use", "redistribution"}
DEMAND_KEYS = {"data_type", "purpose", "license", "units", "max_unit_price", "max_total_price",
               "max_age_seconds", "max_refresh_seconds", "response_seconds", "ttl_seconds"}
SUPPLY_KEYS = {"name", "data_type", "version", "unit_price", "floor_price", "discount_bps",
               "discount_min_units", "min_units", "max_units", "purposes", "licenses", "updated_at",
               "refresh_seconds", "response_seconds", "ttl_seconds"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS demands (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id),
 data_type TEXT NOT NULL, body TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS supplies (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id),
 data_type TEXT NOT NULL, body TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS matches (
 id TEXT PRIMARY KEY, demand_id TEXT NOT NULL REFERENCES demands(id),
 supply_id TEXT NOT NULL REFERENCES supplies(id), terms_hash TEXT NOT NULL,
 body TEXT NOT NULL, expires INTEGER NOT NULL, UNIQUE(demand_id,supply_id,terms_hash));
CREATE INDEX IF NOT EXISTS demand_type ON demands(data_type,expires);
CREATE INDEX IF NOT EXISTS supply_type ON supplies(data_type,expires);
CREATE INDEX IF NOT EXISTS match_expiry ON matches(expires);
CREATE TABLE IF NOT EXISTS negotiations (
 demand_id TEXT NOT NULL REFERENCES demands(id), supply_id TEXT NOT NULL REFERENCES supplies(id),
 body TEXT NOT NULL, expires INTEGER NOT NULL, PRIMARY KEY(demand_id,supply_id));
"""


def payment_asset(value):
    if value == "TEST_CREDIT":
        return value
    if not isinstance(value, str) or re.fullmatch(r"eip155:[1-9][0-9]{0,31}/erc20:0x[0-9a-fA-F]{40}", value) is None:
        raise MachineError("explicit supported settlement asset required")
    network, asset = value.split("/erc20:")
    return network + "/erc20:" + address(asset)


def data_type(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9.-]{1,60}", value) is None:
        raise MachineError("invalid machine-readable data type")
    return value


def normalize_demand(raw):
    require_keys({k: v for k, v in raw.items() if k != "payment_asset"}, DEMAND_KEYS, "buyer demand")
    if (not isinstance(raw["purpose"], str) or not isinstance(raw["license"], str)
            or raw["purpose"] not in PURPOSES or raw["license"] not in LICENSES):
        raise MachineError("unsupported purpose or license")
    unit, total = money_atoms(raw["max_unit_price"]), money_atoms(raw["max_total_price"])
    if unit <= 0 or total <= 0:
        raise MachineError("buyer price limits must be positive")
    return {"data_type": data_type(raw["data_type"]), "purpose": raw["purpose"], "license": raw["license"],
        "payment_asset": payment_asset(raw.get("payment_asset", "TEST_CREDIT")),
        "units": bounded_int(raw["units"], 1, 10000, "quantity"), "max_unit_atoms": unit,
        "max_total_atoms": total,
        "max_age_seconds": bounded_int(raw["max_age_seconds"], 1, 86400, "data age"),
        "max_refresh_seconds": bounded_int(raw["max_refresh_seconds"], 1, 86400, "refresh interval"),
        "response_seconds": bounded_int(raw["response_seconds"], 1, 3600, "response deadline"),
        "ttl_seconds": bounded_int(raw["ttl_seconds"], 30, 86400, "demand lifetime")}


def normalize_supply(raw, now):
    require_keys({k: v for k, v in raw.items() if k != "payment_asset"}, SUPPLY_KEYS, "seller rules")
    for key in ["name", "version"]:
        if not isinstance(raw[key], str) or not 1 <= len(raw[key]) <= 80:
            raise MachineError("invalid seller name or data version")
    for key, allowed in [("purposes", PURPOSES), ("licenses", LICENSES)]:
        if (not isinstance(raw[key], list) or not raw[key]
                or any(not isinstance(v, str) or v not in allowed for v in raw[key])
                or len(set(raw[key])) != len(raw[key])):
            raise MachineError("invalid seller purposes or licenses")
    ask, floor = money_atoms(raw["unit_price"]), money_atoms(raw["floor_price"])
    if not 0 < floor <= ask:
        raise MachineError("invalid seller price range")
    low = bounded_int(raw["min_units"], 1, 10000, "minimum units")
    high = bounded_int(raw["max_units"], low, 10000, "maximum units")
    updated = bounded_int(raw["updated_at"], 1, now + 5, "data update time")
    return {"name": raw["name"], "data_type": data_type(raw["data_type"]), "version": raw["version"],
        "payment_asset": payment_asset(raw.get("payment_asset", "TEST_CREDIT")),
        "ask_atoms": ask, "floor_atoms": floor,
        "discount_bps": bounded_int(raw["discount_bps"], 0, 9000, "discount"),
        "discount_min_units": bounded_int(raw["discount_min_units"], 1, 10000, "discount quantity"),
        "min_units": low, "max_units": high, "purposes": sorted(raw["purposes"]),
        "licenses": sorted(raw["licenses"]), "updated_at": updated,
        "refresh_seconds": bounded_int(raw["refresh_seconds"], 1, 86400, "refresh interval"),
        "response_seconds": bounded_int(raw["response_seconds"], 1, 3600, "response time"),
        "ttl_seconds": bounded_int(raw["ttl_seconds"], 30, 86400, "seller-rule lifetime")}


def negotiate(demand, supply, now):
    reasons = []
    for ok, code in [
        (demand["data_type"] == supply["data_type"], "DATA_TYPE_MISMATCH"),
        (demand.get("payment_asset", "TEST_CREDIT") == supply.get("payment_asset", "TEST_CREDIT"), "ASSET_MISMATCH"),
        (demand["purpose"] in supply["purposes"], "PURPOSE_NOT_GRANTED"),
        (demand["license"] in supply["licenses"], "LICENSE_NOT_GRANTED"),
        (supply["min_units"] <= demand["units"] <= supply["max_units"], "QUANTITY_OUTSIDE_RULES"),
        (-5 <= now - supply["updated_at"] < demand["max_age_seconds"], "DATA_NOT_FRESH"),
        (supply["refresh_seconds"] <= demand["max_refresh_seconds"], "REFRESH_TOO_SLOW"),
        (supply["response_seconds"] <= demand["response_seconds"], "RESPONSE_TOO_SLOW"),
    ]:
        if not ok:
            reasons.append(code)
    discounted = supply["ask_atoms"]
    if demand["units"] >= supply["discount_min_units"]:
        discounted = supply["ask_atoms"] * (10000 - supply["discount_bps"]) // 10000
    counter = max(supply["floor_atoms"], discounted)
    total = counter * demand["units"]
    trace = [{"actor": "seller", "kind": "OFFER", "unit_price": money_string(supply["ask_atoms"])},
             {"actor": "seller", "kind": "POLICY_COUNTER", "unit_price": money_string(counter),
              "units": demand["units"], "license": demand["license"]}]
    if counter > demand["max_unit_atoms"] or total > demand["max_total_atoms"]:
        reasons.append("PRICE_OUTSIDE_BUYER_POLICY")
    if reasons:
        return {"status": "ESCALATE", "reason_codes": reasons, "trace": trace,
                "language_model_calls": 0}
    terms = {"schema_version": "machine-trade-terms-1", "data_type": demand["data_type"],
        "data_version": supply["version"], "data_updated_at": supply["updated_at"],
        "purpose": demand["purpose"], "license": demand["license"], "units": demand["units"],
        "unit_price": money_string(counter), "total_price": money_string(total),
        "asset": demand.get("payment_asset", "TEST_CREDIT"), "refresh_seconds": supply["refresh_seconds"],
        "response_seconds": supply["response_seconds"]}
    trace.append({"actor": "buyer", "kind": "POLICY_ACCEPT", "total_price": terms["total_price"]})
    return {"status": "AGREED", "terms": terms, "terms_hash": digest(terms), "trace": trace,
            "reason_codes": [], "language_model_calls": 0, "payment_status": "NOT_REQUESTED"}


class Market:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript(SCHEMA)

    def register(self, sid, kind, raw):
        if kind not in {"demand", "supply"}:
            raise MachineError("unknown market policy kind")
        now = self.store.clock()
        body = normalize_demand(raw) if kind == "demand" else normalize_supply(raw, now)
        table = "demands" if kind == "demand" else "supplies"
        rid = kind + "-" + secrets.token_hex(12)
        with self.store.connect() as db:
            self._active(db, sid, now)
            if db.execute(f"SELECT COUNT(*) FROM {table} WHERE data_type=? AND expires>?", (
                    body["data_type"], now)).fetchone()[0] >= 100:
                raise MachineError("active policies per data type reached the admission limit")
            db.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?)", (
                rid, sid, body["data_type"], canonical(body).decode(), now + body["ttl_seconds"]))
            self.store._event(db, rid, "DEMAND_REGISTERED" if kind == "demand" else "SELLER_RULE_REGISTERED",
                {"record_id": rid, "owner": sid, "policy_hash": digest(body), "at": now})
            matches = self._match(db, body["data_type"], now,
                                  demand_id=rid if kind == "demand" else None,
                                  supply_id=rid if kind == "supply" else None)
            result = {"id": rid, "policy_hash": digest(body), "matching": matches}
            if kind == "demand":
                result["matches"] = self._demand_matches(db, rid, now)
            return result

    def _match(self, db, kind, now, demand_id=None, supply_id=None):
        # Only the changed record and its indexed counterpart enter negotiation.
        demands = db.execute("SELECT * FROM demands WHERE data_type=? AND expires>?"
            + (" AND id=?" if demand_id else ""),
            (kind, now) + ((demand_id,) if demand_id else ())).fetchall()
        supplies = db.execute("SELECT * FROM supplies WHERE data_type=? AND expires>?"
            + (" AND id=?" if supply_id else ""),
            (kind, now) + ((supply_id,) if supply_id else ())).fetchall()
        evaluated, agreed = 0, 0
        for demand in demands:
            for supply in supplies:
                if demand["owner"] == supply["owner"]:
                    continue
                evaluated += 1
                result = negotiate(json.loads(demand["body"]), json.loads(supply["body"]), now)
                expiry = min(demand["expires"], supply["expires"])
                db.execute("INSERT INTO negotiations VALUES (?,?,?,?) ON CONFLICT(demand_id,supply_id) "
                    "DO UPDATE SET body=excluded.body,expires=excluded.expires", (
                        demand["id"], supply["id"], canonical(result).decode(), expiry))
                if result["status"] != "AGREED":
                    self.store._event(db, "negotiation-" + digest({"demand": demand["id"],
                        "supply": supply["id"], "result": result}), "NEGOTIATION_ESCALATED", {
                        "demand_id": demand["id"], "supply_id": supply["id"],
                        "reason_codes": result["reason_codes"]})
                    continue
                agreed += 1
                terms = {**result["terms"], "buyer_id": demand["owner"], "seller_id": supply["owner"],
                         "demand_id": demand["id"], "supply_id": supply["id"]}
                thash = digest(terms)
                match_id = "match-" + thash[:32]
                # Freshness changes expire this agreement even if both policies remain active.
                db.execute("INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?,?)", (
                    match_id, demand["id"], supply["id"], thash,
                    canonical({**result, "terms": terms, "terms_hash": thash}).decode(),
                    min(demand["expires"], supply["expires"], terms["data_updated_at"]
                        + json.loads(demand["body"])["max_age_seconds"])))
                self.store._event(db, match_id, "TERMS_AGREED", {"match_id": match_id,
                    "terms_hash": thash, "buyer_id": demand["owner"], "seller_id": supply["owner"]})
        return {"trigger": "POLICY_EVENT", "data_type": kind, "pairs_evaluated": evaluated,
                "compatible_pairs": agreed, "language_model_calls": 0}

    def refresh_supply(self, sid, supply_id, version):
        if not isinstance(version, str) or not 1 <= len(version) <= 80:
            raise MachineError("invalid updated data version")
        with self.store.connect() as db:
            now = self.store.clock()
            self._active(db, sid, now)
            row = db.execute("SELECT * FROM supplies WHERE id=? AND owner=?", (supply_id, sid)).fetchone()
            if not row or row["expires"] <= now:
                raise MachineError("active owned seller rule required")
            prior = json.loads(row["body"])
            if version == prior["version"]:
                # Replaying an update must not renew the age of unchanged data.
                return {"trigger": "DUPLICATE_VERSION", "data_type": row["data_type"],
                        "pairs_evaluated": 0, "compatible_pairs": 0, "language_model_calls": 0}
            body = {**prior, "version": version, "updated_at": now}
            db.execute("UPDATE supplies SET body=? WHERE id=?", (canonical(body).decode(), supply_id))
            # Agreements for the previous data version are no longer admitted.
            db.execute("UPDATE matches SET expires=MIN(expires,?) WHERE supply_id=?", (now, supply_id))
            self.store._event(db, supply_id + ":" + digest(body), "DATA_UPDATED", {
                "supply_id": supply_id, "version": version, "updated_at": body["updated_at"]})
            return self._match(db, row["data_type"], now, supply_id=supply_id)

    def _active(self, db, sid, now):
        row = db.execute("SELECT expires FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row or row["expires"] <= now:
            raise MachineError("active participant required")

    def admit(self, sid, match_id, terms_hash):
        """Re-check the agreed version at the payment boundary, without signing."""
        self.agreement(sid, match_id, terms_hash)
        with self.store.connect() as db:
            # TEST_CREDIT has no conversion to a real token. Never fabricate an x402 proof.
            self.store._event(db, match_id + ":payment-request", "PAYMENT_ADMISSION_BLOCKED", {
                "match_id": match_id, "terms_hash": terms_hash, "reason": "REAL_PAYMENT_NOT_CONFIGURED"})
            return {"status": "BLOCKED", "reason": "REAL_PAYMENT_NOT_CONFIGURED",
                    "terms_hash": terms_hash, "payment_status": "NOT_REQUESTED",
                    "signing_authority": "NONE", "tx_hash": None}

    def agreement(self, sid, match_id, terms_hash, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.agreement(sid, match_id, terms_hash, connection)
        now = self.store.clock()
        self._active(db, sid, now)
        row = db.execute("SELECT m.*,s.body AS supply_body,s.expires AS supply_expiry,"
            "d.expires AS demand_expiry FROM matches m JOIN supplies s ON s.id=m.supply_id "
            "JOIN demands d ON d.id=m.demand_id WHERE m.id=? AND d.owner=?", (match_id, sid)).fetchone()
        if not row or min(row["expires"], row["supply_expiry"], row["demand_expiry"]) <= now:
            raise MachineError("active buyer agreement required")
        result, supply = json.loads(row["body"]), json.loads(row["supply_body"])
        if row["terms_hash"] != terms_hash or digest(result["terms"]) != terms_hash:
            raise MachineError("agreed terms hash required")
        if (supply["version"] != result["terms"]["data_version"]
                or supply["updated_at"] != result["terms"]["data_updated_at"]):
            raise MachineError("data changed since agreement")
        return {"id": row["id"], "expires": row["expires"], **result}

    def demand_matches(self, sid, demand_id):
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM demands WHERE id=? AND owner=?", (demand_id, sid)).fetchone()
            if not row:
                raise MachineError("owned demand required")
            return {"matches": self._demand_matches(db, demand_id, self.store.clock())}

    def _demand_matches(self, db, demand_id, now):
        return [{"id": r["id"], **json.loads(r["body"]), "expires": r["expires"]}
            for r in db.execute("SELECT * FROM matches WHERE demand_id=? AND expires>? "
                "ORDER BY rowid DESC LIMIT 100", (demand_id, now))]

    def tick(self):
        """Timer events invalidate agreements; timers never invoke an LLM."""
        now, count = self.store.clock(), 0
        with self.store.connect() as db:
            for row in db.execute("SELECT m.id,m.terms_hash,m.expires FROM matches m "
                    "WHERE m.expires<=? AND NOT EXISTS (SELECT 1 FROM events e "
                    "WHERE e.event_id=m.id||':expired')", (now,)).fetchall():
                self.store._event(db, row["id"] + ":expired", "AGREEMENT_EXPIRED", {
                    "match_id": row["id"], "terms_hash": row["terms_hash"], "expired_at": row["expires"]})
                count += 1
        return {"expired_agreements": count, "language_model_calls": 0}

    def snapshot(self, sid):
        now = self.store.clock()
        with self.store.connect() as db:
            demands = [{"id": r["id"], **json.loads(r["body"]), "expires": r["expires"]}
                       for r in db.execute("SELECT * FROM demands WHERE owner=? ORDER BY rowid DESC", (sid,))]
            supplies = []
            for row in db.execute("SELECT * FROM supplies WHERE expires>? ORDER BY rowid DESC", (now,)):
                body = json.loads(row["body"])
                public = {k: v for k, v in body.items() if k not in
                          {"floor_atoms", "discount_bps", "discount_min_units"}}
                supplies.append({"id": row["id"], "owner": row["owner"], **public,
                                 "unit_price": money_string(body["ask_atoms"])})
            matches = [{"id": r["id"], **json.loads(r["body"]), "expires": r["expires"]}
                for r in db.execute("SELECT m.* FROM matches m JOIN demands d ON d.id=m.demand_id "
                    "JOIN supplies s ON s.id=m.supply_id WHERE (d.owner=? OR s.owner=?) AND m.expires>? "
                    "ORDER BY m.rowid DESC", (sid, sid, now))]
            deferred = [{"demand_id": r["demand_id"], "supply_id": r["supply_id"],
                         **json.loads(r["body"])} for r in db.execute(
                "SELECT n.* FROM negotiations n JOIN demands d ON d.id=n.demand_id "
                "JOIN supplies s ON s.id=n.supply_id WHERE (d.owner=? OR s.owner=?) AND n.expires>?",
                (sid, sid, now)) if json.loads(r["body"])["status"] == "ESCALATE"]
            return {"demands": demands, "supplies": supplies, "matches": matches, "deferred": deferred,
                    "language_model_calls": 0, "payment_status": "NOT_REQUESTED"}
