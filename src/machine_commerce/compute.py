"""Bound inference requests to the existing checkout, budget and x402 ledger.

Provider output is validated and locally wrapped, not certified as model truth.
No signer, refund promise, free-credit key or GPU-container lease is created.
"""

import hashlib
import json
import secrets

from economic_machine.values import MachineError, canonical, digest, require_keys

from .compute_registry import NETWORK, URL, USDC
from .domain import money_atoms, money_string
from .x402 import address, decode_header

MODELS = {"llama-3.1-8b", "llama-3.1-70b", "qwen-2.5-7b", "mistral-small"}


def inference_request(raw):
    require_keys(raw, {"model", "messages", "max_tokens"}, "inference request")
    if not isinstance(raw["model"], str) or raw["model"] not in MODELS or type(raw["max_tokens"]) is not int or not 1 <= raw["max_tokens"] <= 2048:
        raise MachineError("BOUNDED_INFERENCE_MODEL_AND_TOKENS_REQUIRED")
    if not isinstance(raw["messages"], list) or not 1 <= len(raw["messages"]) <= 32:
        raise MachineError("BOUNDED_INFERENCE_MESSAGES_REQUIRED")
    for message in raw["messages"]:
        require_keys(message, {"role", "content"}, "inference message")
        if (not isinstance(message["role"], str) or message["role"] not in {"user", "assistant", "system"} or not isinstance(message["content"], str)
                or not 1 <= len(message["content"]) <= 16000):
            raise MachineError("BOUNDED_INFERENCE_MESSAGE_REQUIRED")
    if len(canonical(raw)) > 24000:
        raise MachineError("INFERENCE_REQUEST_TOO_LARGE")
    return json.loads(canonical(raw))


def payment_option(headers, profile, maximum):
    body = decode_header(headers.get("payment-required"))
    if body.get("x402Version") != 2 or body.get("resource", {}).get("url") != URL:
        raise MachineError("COMPUTE_PROVIDER_CHALLENGE_MISMATCH")
    options = body.get("accepts")
    if not isinstance(options, list) or not 1 <= len(options) <= 16:
        raise MachineError("BOUNDED_COMPUTE_CHALLENGE_REQUIRED")
    selected = [p for p in options if isinstance(p, dict) and p.get("network") == NETWORK and p.get("scheme") == "exact"]
    if len(selected) != 1:
        raise MachineError("UNAMBIGUOUS_COMPUTE_REQUIREMENT_REQUIRED")
    option = selected[0]
    value = option.get("amount")
    if (not isinstance(value, str) or not value.isascii() or not value.isdigit()
            or len(value) > 8 or str(int(value)) != value or not 0 < int(value) <= maximum
            or address(option.get("asset")) != USDC or address(option.get("payTo")) != address(profile["pay_to"])
            or option.get("extra") != {"name": "USD Coin", "version": "2"}
            or type(option.get("maxTimeoutSeconds")) is not int or not 1 <= option["maxTimeoutSeconds"] <= 3600):
        raise MachineError("COMPUTE_PRICE_RECIPIENT_OR_DOMAIN_MISMATCH")
    return int(value)


class ComputeTransport:
    def __init__(self, store, delegate, resources):
        self.store, self.delegate, self.resources = store, delegate, resources
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS compute_jobs (checkout_id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                       "resource_id TEXT NOT NULL, request TEXT NOT NULL, request_hash TEXT NOT NULL, "
                       "input_hash TEXT NOT NULL, created INTEGER NOT NULL)")

    def call(self, url, body, headers=None):
        headers = headers or {}
        if url != URL:
            return self.delegate.call(url, body, headers)
        pid = headers.get("Idempotency-Key")
        with self.store.connect() as db:
            row = db.execute("SELECT j.*,p.body AS payment_body FROM compute_jobs j JOIN commerce_checkouts c "
                             "ON c.id=j.checkout_id JOIN payments p ON p.id=c.payment_id "
                             "WHERE p.id=? AND p.owner=j.owner AND p.status IN "
                             "('PREPARED','CHALLENGE_READY','SUBMITTED')", (pid,)).fetchone()
        if not row:
            raise MachineError("BOUND_COMPUTE_CHECKOUT_REQUIRED")
        payment = json.loads(row["payment_body"])
        request = json.loads(row["request"])
        if (row["resource_id"] not in self.resources or canonical(body) != canonical(payment["request"])
                or digest(request) != row["request_hash"]):
            raise MachineError("COMPUTE_REQUEST_BINDING_CHANGED")
        signed = "PAYMENT-SIGNATURE" in headers
        status, response_headers, content = self.delegate.call(url, request, headers | {"x-payment-network": NETWORK})
        if not signed or not 200 <= status < 300:
            return status, response_headers, content
        value = json.loads(content)
        # Gate402 documents text + camelCase usage, rather than the upstream
        # OpenAI choices envelope. Normalize only these explicit fields; its
        # floating monetary claims and HMAC receipt never prove a refund.
        if isinstance(value, dict) and isinstance(value.get("text"), str) and isinstance(value.get("usage"), dict):
            source_usage = value["usage"]
            value = {"model": value.get("model"), "choices": [{"message": {"role": "assistant", "content": value["text"]},
                "finish_reason": "provider-reported"}], "usage": {
                    "prompt_tokens": source_usage.get("promptTokens"), "completion_tokens": source_usage.get("completionTokens"),
                    "total_tokens": source_usage.get("totalTokens")}}
        if (not isinstance(value, dict) or value.get("model") != request["model"]
                or not isinstance(value.get("choices"), list) or not 1 <= len(value["choices"]) <= 4):
            raise MachineError("COMPUTE_DELIVERY_SCHEMA_MISMATCH")
        choices = []
        for choice in value["choices"]:
            message = choice.get("message") if isinstance(choice, dict) else None
            if (not isinstance(message, dict) or message.get("role") != "assistant"
                    or not isinstance(message.get("content"), str) or len(message["content"]) > 64000):
                raise MachineError("COMPUTE_DELIVERY_SCHEMA_MISMATCH")
            choices.append({"message": {"role": "assistant", "content": message["content"]},
                            "finish_reason": str(choice.get("finish_reason", "unknown"))[:40]})
        usage = value.get("usage")
        if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or not 0 <= usage[k] <= 100000
                for k in ["prompt_tokens", "completion_tokens", "total_tokens"])
                or usage["completion_tokens"] > request["max_tokens"]
                or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
            raise MachineError("COMPUTE_USAGE_SCHEMA_MISMATCH")
        output = {"model": value["model"], "choices": choices,
            "usage": {k: usage[k] for k in ["prompt_tokens", "completion_tokens", "total_tokens"]},
            "request_sha256": row["request_hash"], "provider_response_sha256": hashlib.sha256(content).hexdigest(),
            "receipt_assurance": "LOCAL_REQUEST_BINDING_PROVIDER_REPORTED_USAGE_NOT_MODEL_TRUTH",
            "refund_confirmed": False, "gpu_capacity_verified": False}
        artifact = {"terms_hash": payment["binding"]["terms_hash"], "data_version": body["data_version"], "data": output}
        return status, response_headers, canonical(artifact)


class ComputeService:
    def __init__(self, checkout, transport, resource_ids=None):
        self.checkout, self.store = checkout, checkout.store
        self.transport = transport
        self.resources = set(resource_ids or [])
        for rid in self.resources:
            p = checkout.payments.profiles.get(rid)
            if (not p or p["url"] != URL or p["network"] != NETWORK or address(p["asset"]) != USDC
                    or p["data_type"] != "compute.inference" or p["seller_owner"] != "compute-" + rid):
                raise MachineError("APPROVED_COMPUTE_RESOURCE_REQUIRED")
        previous = checkout.payments.transport
        if isinstance(previous, ComputeTransport):
            previous = previous.delegate
        self.adapter = ComputeTransport(self.store, previous, self.resources)
        checkout.payments.transport = self.adapter
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS compute_publishers (owner TEXT PRIMARY KEY, resource_id TEXT UNIQUE NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS compute_quote_intents (owner TEXT NOT NULL, request_id TEXT NOT NULL, "
                       "input_hash TEXT NOT NULL, checkout_request TEXT NOT NULL, PRIMARY KEY(owner,request_id))")

    def quote(self, sid, raw):
        require_keys(raw, {"resource_id", "request", "max_total", "idempotency_key"}, "compute quote")
        from .domain import identifier
        identifier(raw["resource_id"], "compute resource ID")
        identifier(raw["idempotency_key"], "compute idempotency key")
        rid = raw["resource_id"]
        if rid not in self.resources:
            raise MachineError("COMPUTE_PURCHASING_NOT_CONFIGURED")
        request = inference_request(raw["request"])
        fingerprint = digest(raw)
        with self.store.connect() as db:
            self.checkout.market._active(db, sid, self.store.clock())
            prior = db.execute("SELECT j.* FROM compute_jobs j JOIN commerce_checkouts c ON c.id=j.checkout_id "
                               "WHERE j.owner=? AND c.idempotency_key=?", (sid, raw["idempotency_key"])).fetchone()
            if prior:
                if prior["input_hash"] != fingerprint:
                    raise MachineError("COMPUTE_IDEMPOTENCY_CONFLICT")
                cid = prior["checkout_id"]
            else:
                cid = None
        if cid:
            return self.checkout.get(sid, cid) | {"request_sha256": digest(request), "kind": "INFERENCE_API"}
        maximum = money_atoms(raw["max_total"])
        if not 0 < maximum <= 10_000_000:
            raise MachineError("BOUNDED_COMPUTE_SPEND_REQUIRED")
        profile = self.checkout.payments.profiles[rid]
        with self.store.connect() as db:
            intent = db.execute("SELECT * FROM compute_quote_intents WHERE owner=? AND request_id=?",
                                (sid, raw["idempotency_key"])).fetchone()
            if intent and intent["input_hash"] != fingerprint:
                raise MachineError("COMPUTE_IDEMPOTENCY_CONFLICT")
        if not intent:
            status, headers, _ = self.transport.call(URL, request, {"x-payment-network": NETWORK})
            if status != 402:
                raise MachineError("UNPAID_COMPUTE_CHALLENGE_REQUIRED")
            amount = payment_option(headers, profile, maximum)
        else:
            amount = money_atoms(json.loads(intent["checkout_request"])["max_total"])
        now, owner = self.store.clock(), profile["seller_owner"]
        with self.store.connect() as db:
            intent = db.execute("SELECT * FROM compute_quote_intents WHERE owner=? AND request_id=?",
                                (sid, raw["idempotency_key"])).fetchone()
            if intent:
                if intent["input_hash"] != fingerprint:
                    raise MachineError("COMPUTE_IDEMPOTENCY_CONFLICT")
                quote_request = json.loads(intent["checkout_request"])
            else:
                quote_request = None
        if quote_request is None:
            quote_request = self.publish_quote(sid, raw, profile, request, amount, now, owner, fingerprint)
        quoted = self.checkout.quote(sid, quote_request)
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO compute_jobs VALUES (?,?,?,?,?,?,?)", (quoted["id"], sid, rid,
                canonical(request).decode(), digest(request), fingerprint, now))
            existing = db.execute("SELECT * FROM compute_jobs WHERE checkout_id=?", (quoted["id"],)).fetchone()
            if existing["input_hash"] != fingerprint or existing["request_hash"] != digest(request):
                raise MachineError("COMPUTE_IDEMPOTENCY_CONFLICT")
            self.store._event(db, quoted["id"] + ":compute", "COMPUTE_REQUEST_BOUND", {
                "checkout_id": quoted["id"], "request_sha256": digest(request), "price_atoms": amount})
        return quoted | {"request_sha256": digest(request), "kind": "INFERENCE_API"}

    def publish_quote(self, sid, raw, profile, request, amount, now, owner, fingerprint):
        from .market import normalize_supply
        rid = raw["resource_id"]
        with self.store.connect() as db:
            prior = db.execute("SELECT * FROM compute_quote_intents WHERE owner=? AND request_id=?",
                               (sid, raw["idempotency_key"])).fetchone()
            if prior:
                if prior["input_hash"] != fingerprint:
                    raise MachineError("COMPUTE_IDEMPOTENCY_CONFLICT")
                return json.loads(prior["checkout_request"])
            existing = db.execute("SELECT id FROM sessions WHERE id=?", (owner,)).fetchone()
            registered = db.execute("SELECT resource_id FROM compute_publishers WHERE owner=?", (owner,)).fetchone()
            if existing and (not registered or registered[0] != rid):
                raise MachineError("COMPUTE_PUBLISHER_IDENTITY_CONFLICT")
            if not existing:
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,0,0,0,0)", (owner,
                           hashlib.sha256(secrets.token_bytes(32)).hexdigest(), now, now + 3600))
                db.execute("INSERT INTO compute_publishers VALUES (?,?)", (owner, rid))
            db.execute("UPDATE sessions SET expires=? WHERE id=?", (now + 3600, owner))
            if db.execute("SELECT COUNT(*) FROM supplies WHERE owner=? AND expires>?", (owner, now)).fetchone()[0] >= 50:
                raise MachineError("COMPUTE_QUOTE_CAPACITY_REACHED")
            supply = normalize_supply({"name": "Gate402 " + request["model"], "data_type": profile["data_type"],
                "version": profile["data_version"], "payment_asset": NETWORK + "/erc20:" + USDC,
                "unit_price": money_string(amount), "floor_price": money_string(amount), "discount_bps": 0,
                "discount_min_units": 1, "min_units": 1, "max_units": 1, "purposes": ["automation"],
                "licenses": ["internal-use"], "updated_at": now, "refresh_seconds": 120,
                "response_seconds": 30, "ttl_seconds": 120}, now)
            supply_id = "supply-" + secrets.token_hex(12)
            db.execute("INSERT INTO supplies VALUES (?,?,?,?,?)", (supply_id, owner, profile["data_type"],
                       canonical(supply).decode(), now + 120))
            quote_request = {"resource_id": rid, "supply_id": supply_id, "plan_id": None,
                "units": 1, "purpose": "automation", "license": "internal-use", "max_total": money_string(amount),
                "max_age_seconds": 120, "max_refresh_seconds": 120, "response_seconds": 30,
                "idempotency_key": raw["idempotency_key"]}
            db.execute("INSERT INTO compute_quote_intents VALUES (?,?,?,?)", (sid, raw["idempotency_key"],
                       fingerprint, canonical(quote_request).decode()))
            return quote_request

    def result(self, sid, cid):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM compute_jobs WHERE checkout_id=? AND owner=?", (cid, sid)).fetchone()
            if not row:
                raise MachineError("OWNED_COMPUTE_JOB_REQUIRED")
        checkout = self.checkout.get(sid, cid)
        payment = checkout["payment"]
        if not payment or payment["status"] != "SETTLED" or (payment["observation"] or {}).get("finality") != "finalized":
            raise MachineError("FINALIZED_COMPUTE_PAYMENT_AND_DELIVERY_REQUIRED")
        output = payment["delivery"]["artifact"]["data"]
        if output["request_sha256"] != row["request_hash"]:
            raise MachineError("COMPUTE_DELIVERY_REQUEST_MISMATCH")
        return {"checkout_id": cid, "payment_id": payment["id"], "tx_hash": payment["tx_hash"], "output": output}
