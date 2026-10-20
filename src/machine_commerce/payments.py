"""Noncustodial x402 execution with persistent capital holds and nonce recovery.

The customer signs externally. A signed authorization is sent at most once by
this runtime; ambiguous outcomes retain capital until chain reconciliation.
"""

import json
import re
import secrets
from dataclasses import asdict

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data, hash_domain

from economic_machine.values import MachineError, canonical, digest, require_keys

from .domain import bounded_int, identifier, money_atoms
from .transport import HTTPS, Chain, hash32, https_url, integer
from .x402 import PaymentBinding, address, admit_required, decode_header, inspect_response

SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_mandates (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), payer TEXT NOT NULL,
 payment_asset TEXT NOT NULL, budget INTEGER NOT NULL, max_order INTEGER NOT NULL,
 resources TEXT NOT NULL, expires INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0,
 spent INTEGER NOT NULL DEFAULT 0, CHECK(reserved >= 0 AND spent >= 0 AND reserved+spent <= budget));
CREATE TABLE IF NOT EXISTS payments (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), mandate_id TEXT NOT NULL REFERENCES payment_mandates(id),
 match_id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL, input_hash TEXT NOT NULL,
 body TEXT NOT NULL, status TEXT NOT NULL, signature_hash TEXT, tx_hash TEXT, delivery TEXT,
 observation TEXT, reason TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL,
 UNIQUE(owner,idempotency_key));
"""


def validate_profiles(profiles):
    if not isinstance(profiles, dict) or len(profiles) > 100:
        raise MachineError("bounded operator resource registry required")
    result = {}
    for rid, profile in profiles.items():
        identifier(rid, "resource id")
        require_keys(profile, {"url", "network", "asset", "pay_to", "token_name", "token_version",
            "max_timeout_seconds", "seller_owner", "data_type", "data_version", "rpc_url", "finality"}, "resource profile")
        https_url(profile["url"])
        https_url(profile["rpc_url"])
        if profile["finality"] != "finalized":
            raise MachineError("finalized chain observation is required")
        identifier(profile["seller_owner"], "registered seller owner")
        bounded_int(profile["max_timeout_seconds"], 1, 120, "authorization window")
        if (not isinstance(profile["data_type"], str) or not isinstance(profile["data_version"], str)
                or not 1 <= len(profile["data_version"]) <= 80):
            raise MachineError("registered data type and version required")
        binding = PaymentBinding("0" * 64, profile["url"], profile["network"], profile["asset"],
            profile["pay_to"], 1, 1, profile["max_timeout_seconds"], profile["token_name"], profile["token_version"])
        binding.validate()
        if address(profile["asset"]) == "0x" + "0" * 40 or address(profile["pay_to"]) == "0x" + "0" * 40:
            raise MachineError("zero token or recipient is forbidden")
        result[rid] = dict(profile)
    return result


def typed_authorization(binding, auth):
    return {"types": {"EIP712Domain": [
        {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
        "TransferWithAuthorization": [{"name": name, "type": kind} for name, kind in
            [("from", "address"), ("to", "address"), ("value", "uint256"), ("validAfter", "uint256"),
             ("validBefore", "uint256"), ("nonce", "bytes32")]]},
        "primaryType": "TransferWithAuthorization", "domain": {"name": binding.token_name,
            "version": binding.token_version, "chainId": int(binding.network.split(":")[1]),
            "verifyingContract": binding.asset}, "message": {**auth,
                **{key: int(auth[key]) for key in ["value", "validAfter", "validBefore"]}}}


def validate_signature(header, body, now):
    payload = decode_header(header)
    require_keys(payload, {"x402Version", "resource", "accepted", "payload"}, "signed x402 payment")
    if (type(payload["x402Version"]) is not int or payload["x402Version"] != 2
            or not isinstance(payload["resource"], dict)
            or payload["resource"].get("url") != body["binding"]["resource_url"]
            or canonical(payload["accepted"]) != canonical(body["accepted"])):
        raise MachineError("signed payment differs from admitted challenge")
    require_keys(payload["payload"], {"signature", "authorization"}, "payment payload")
    auth = payload["payload"]["authorization"]
    require_keys(auth, {"from", "to", "value", "validAfter", "validBefore", "nonce"}, "authorization")
    for field in ["value", "validAfter", "validBefore"]:
        if not isinstance(auth[field], str) or re.fullmatch(r"0|[1-9][0-9]{0,77}", auth[field]) is None:
            raise MachineError("canonical authorization integers required")
    normalized = {**auth, "from": address(auth["from"]), "to": address(auth["to"]), "nonce": hash32(auth["nonce"])}
    if normalized != body["authorization"] or not int(auth["validAfter"]) < now < int(auth["validBefore"]):
        raise MachineError("authorization differs from prepared payment or has expired")
    signature = payload["payload"]["signature"]
    if not isinstance(signature, str) or re.fullmatch(r"0x[0-9a-fA-F]{130}", signature) is None:
        raise MachineError("EIP-3009 signature required")
    try:
        recovered = Account.recover_message(encode_typed_data(full_message=typed_authorization(
            PaymentBinding(**body["binding"]), normalized)), signature=signature)
    except (ValueError, TypeError, OverflowError) as exc:
        raise MachineError("invalid payment signature") from exc
    if address(recovered) != normalized["from"]:
        raise MachineError("signature does not belong to approved payer")
    return payload


class Payments:
    def __init__(self, store, market, profiles=None, transport=None, chain=None, *, public_network=None):
        self.store, self.market = store, market
        self.profiles = validate_profiles(profiles or {})
        if public_network is not None and public_network not in {"eip155:42161", "eip155:421614"}:
            raise MachineError("approved public payment network required")
        self.public_network = public_network
        urls = {url for profile in self.profiles.values() for url in [profile["url"], profile["rpc_url"]]}
        self.transport = transport or HTTPS(urls)
        self.chain = chain or Chain(self.transport)
        with store.connect() as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    def mandate(self, sid, raw):
        require_keys(raw, {"payer", "payment_asset", "budget", "max_order", "resources", "ttl_seconds"}, "payment mandate")
        payer = address(raw["payer"])
        resources = raw["resources"]
        if (not isinstance(resources, list) or not resources or any(not isinstance(r, str) or r not in self.profiles for r in resources)
                or len(set(resources)) != len(resources)):
            raise MachineError("choose unique registered payment resources")
        asset = raw["payment_asset"]
        if any(self.profiles[r]["network"] + "/erc20:" + address(self.profiles[r]["asset"]) != asset for r in resources):
            raise MachineError("mandate asset differs from registered resources")
        budget, maximum = money_atoms(raw["budget"]), money_atoms(raw["max_order"])
        if not 0 < maximum <= budget:
            raise MachineError("positive payment budget and bounded per-order limit required")
        ttl = bounded_int(raw["ttl_seconds"], 60, 86400, "mandate lifetime")
        mid, now = "mandate-" + secrets.token_hex(12), self.store.clock()
        with self.store.connect() as db:
            self.market._active(db, sid, now)
            expiry = min(now + ttl, db.execute("SELECT expires FROM sessions WHERE id=?", (sid,)).fetchone()[0])
            db.execute("INSERT INTO payment_mandates VALUES (?,?,?,?,?,?,?,?,0,0)", (
                mid, sid, payer, asset, budget, maximum, canonical(sorted(resources)).decode(), expiry))
            self.store._event(db, mid, "PAYMENT_MANDATE_CREATED", {
                "mandate_id": mid, "owner": sid, "payer": payer, "payment_asset": asset,
                "budget_atoms": budget, "max_order_atoms": maximum, "resources": resources, "expires": expiry})
        return {"id": mid, "payer": payer, "payment_asset": asset, "budget_atoms": budget,
                "max_order_atoms": maximum, "expires": expiry, "signing_authority": "CUSTOMER_EXTERNAL"}

    def prepare(self, sid, raw):
        require_keys(raw, {"match_id", "terms_hash", "mandate_id", "resource_id", "idempotency_key"}, "payment preparation")
        for key in ["match_id", "mandate_id", "resource_id", "idempotency_key"]:
            identifier(raw[key], key)
        pid, now = "payment-" + secrets.token_hex(12), self.store.clock()
        request_hash = digest(raw)
        with self.store.connect() as db:
            existing = db.execute("SELECT * FROM payments WHERE owner=? AND idempotency_key=?", (sid, raw["idempotency_key"])).fetchone()
            if existing:
                if existing["input_hash"] != request_hash:
                    raise MachineError("payment idempotency key reused with different intent")
                return self._public(existing)
            agreement = self.market.agreement(sid, raw["match_id"], raw["terms_hash"], db)
            if db.execute("SELECT id FROM payments WHERE match_id=?", (raw["match_id"],)).fetchone():
                raise MachineError("this agreement already has a payment; reconcile its existing record")
            mandate = db.execute("SELECT * FROM payment_mandates WHERE id=? AND owner=?", (raw["mandate_id"], sid)).fetchone()
            profile = self.profiles.get(raw["resource_id"])
            if profile and self.public_network and profile["network"] != self.public_network:
                raise MachineError("historical payment resource; only reconciliation remains available")
            if (not mandate or mandate["expires"] <= now or not profile
                    or raw["resource_id"] not in json.loads(mandate["resources"])):
                raise MachineError("active owned mandate and approved resource required")
            terms = agreement["terms"]
            if (terms["seller_id"] != profile["seller_owner"] or terms["data_type"] != profile["data_type"]
                    or terms["data_version"] != profile["data_version"] or terms["asset"] == "TEST_CREDIT"
                    or terms["asset"] != mandate["payment_asset"]):
                raise MachineError("agreement is not denominated in the approved real asset and resource")
            amount = money_atoms(terms["total_price"])
            if amount > mandate["max_order"] or mandate["reserved"] + mandate["spent"] + amount > mandate["budget"]:
                raise MachineError("payment mandate cap exceeded")
            expiry = min(agreement["expires"], mandate["expires"], now + profile["max_timeout_seconds"])
            if expiry <= now + 5:
                raise MachineError("insufficient authorization lifetime")
            binding = PaymentBinding(raw["terms_hash"], profile["url"], profile["network"], address(profile["asset"]),
                address(profile["pay_to"]), amount, expiry, profile["max_timeout_seconds"], profile["token_name"], profile["token_version"])
            binding.validate()
            body = {"resource_id": raw["resource_id"], "profile_hash": digest(profile), "binding": asdict(binding),
                "authorization": {"from": mandate["payer"], "to": binding.pay_to, "value": str(amount),
                    "validAfter": str(max(0, now - 5)), "validBefore": str(expiry), "nonce": "0x" + secrets.token_hex(32)},
                "request": {"terms_hash": raw["terms_hash"], "data_version": terms["data_version"],
                    "units": terms["units"], "purpose": terms["purpose"], "license": terms["license"]}}
            db.execute("UPDATE payment_mandates SET reserved=reserved+? WHERE id=?", (amount, mandate["id"]))
            db.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,'PREPARED',NULL,NULL,NULL,NULL,NULL,?,?)", (
                pid, sid, mandate["id"], raw["match_id"], raw["idempotency_key"], request_hash, canonical(body).decode(), now, now))
            self.store._event(db, pid + ":prepared", "PAYMENT_PREPARED", {
                "payment_id": pid, "mandate_id": mandate["id"], "terms_hash": binding.terms_hash,
                "amount_atoms": amount, "nonce": body["authorization"]["nonce"]})
            return self._public(db.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone())

    def _row(self, db, sid, pid):
        row = db.execute("SELECT * FROM payments WHERE id=? AND owner=?", (pid, sid)).fetchone()
        if not row:
            raise MachineError("owned payment required")
        return row

    def _profile(self, body):
        profile = self.profiles.get(body["resource_id"])
        if not profile or digest(profile) != body["profile_hash"]:
            raise MachineError("resource registry changed; payment admission stopped")
        return profile

    def _public(self, row):
        body = json.loads(row["body"])
        binding = PaymentBinding(**body["binding"])
        result = {"id": row["id"], "mandate_id": row["mandate_id"], "match_id": row["match_id"],
            "status": row["status"], "reason": row["reason"], "terms_hash": binding.terms_hash,
            "amount_atoms": str(binding.amount_atoms), "network": binding.network, "asset": binding.asset,
            "payer": body["authorization"]["from"], "pay_to": binding.pay_to, "tx_hash": row["tx_hash"],
            "created": row["created"], "expires": binding.expires, "signing_authority": "CUSTOMER_EXTERNAL",
            "safe_to_retry_payment": False, "delivery": json.loads(row["delivery"]) if row["delivery"] else None,
            "observation": json.loads(row["observation"]) if row["observation"] else None}
        if row["status"] == "CHALLENGE_READY":
            result.update(typed_data=typed_authorization(binding, body["authorization"]),
                payment_template={"x402Version": 2, "resource": {"url": binding.resource_url},
                    "accepted": body["accepted"], "payload": {"authorization": body["authorization"]}},
                signature_required=True)
        return result

    def get(self, sid, pid):
        with self.store.connect() as db:
            return self._public(self._row(db, sid, pid))

    def snapshot(self, sid):
        with self.store.connect() as db:
            return {"payments": [self._public(r) for r in db.execute("SELECT * FROM payments WHERE owner=? ORDER BY rowid DESC LIMIT 100", (sid,))],
                "mandates": [{k: (json.loads(r[k]) if k == "resources" else r[k]) for k in
                    ["id", "payer", "payment_asset", "budget", "max_order", "resources", "expires", "reserved", "spent"]}
                    for r in db.execute("SELECT * FROM payment_mandates WHERE owner=? ORDER BY rowid DESC LIMIT 100", (sid,))],
                "registered_resources": list(self.profiles),
                "resource_details": [{"id": rid, "payment_asset": p["network"] + "/erc20:" + address(p["asset"]),
                    "network": p["network"], "pay_to": p["pay_to"], "data_type": p["data_type"],
                    "data_version": p["data_version"], "decimals": 6} for rid, p in self.profiles.items()],
                "custody": "NONE"}

    def challenge(self, sid, pid):
        with self.store.connect() as db:
            row = self._row(db, sid, pid)
            if row["status"] == "CHALLENGE_READY":
                return self._public(row)
            if row["status"] != "PREPARED":
                raise MachineError("unsigned prepared payment required")
            body = json.loads(row["body"])
            self.market.agreement(sid, row["match_id"], body["binding"]["terms_hash"], db)
            profile = self._profile(body)
        try:
            status, headers, _ = self.transport.call(profile["url"], body["request"], {"Idempotency-Key": pid})
        except (httpx.HTTPError, OSError) as exc:
            raise MachineError("resource challenge unavailable; no signature was sent") from exc
        if status != 402 or not headers.get("payment-required"):
            raise MachineError("registered resource did not return an x402 challenge")
        binding = PaymentBinding(**body["binding"])
        admitted = admit_required(headers["payment-required"], binding, binding.terms_hash, self.store.clock())
        body["accepted"] = admitted["accepted"]
        body["authorization"]["validBefore"] = str(admitted["authorization_expires"])
        body["binding"]["expires"] = admitted["authorization_expires"]
        if integer(self.chain.rpc(profile["rpc_url"], "eth_chainId", [])) != int(profile["network"].split(":")[1]):
            raise MachineError("challenge observation chain differs from the registry")
        code = self.chain.rpc(profile["rpc_url"], "eth_getCode", [profile["asset"], "latest"])
        if not isinstance(code, str) or code == "0x":
            raise MachineError("registered payment token has no contract")
        decimals = self.chain.rpc(profile["rpc_url"], "eth_call", [{"to": profile["asset"], "data": "0x313ce567"}, "latest"])
        if integer(decimals) != 6:
            raise MachineError("this payment adapter requires an explicitly verified six-decimal token")
        domain = typed_authorization(binding, body["authorization"])["domain"]
        separator = self.chain.rpc(profile["rpc_url"], "eth_call", [{"to": profile["asset"], "data": "0x3644e515"}, "latest"])
        if not isinstance(separator, str) or separator.lower() != "0x" + hash_domain(domain).hex():
            raise MachineError("registered token domain differs from its chain contract")
        balance = self.chain.rpc(profile["rpc_url"], "eth_call", [{"to": profile["asset"],
            "data": "0x70a08231" + body["authorization"]["from"][2:].rjust(64, "0")}, "latest"])
        if integer(balance) < binding.amount_atoms:
            raise MachineError("payer token balance cannot fund this payment")
        body["start_block"] = integer(self.chain.rpc(profile["rpc_url"], "eth_blockNumber", []))
        with self.store.connect() as db:
            current = self._row(db, sid, pid)
            if current["status"] != "PREPARED":
                return self._public(current)
            self.market.agreement(sid, row["match_id"], binding.terms_hash, db)
            db.execute("UPDATE payments SET body=?,status='CHALLENGE_READY',updated=? WHERE id=?", (
                canonical(body).decode(), self.store.clock(), pid))
            self.store._event(db, pid + ":challenge", "PAYMENT_CHALLENGE_ADMITTED", {
                "payment_id": pid, "challenge_hash": admitted["challenge_hash"]})
            return self._public(self._row(db, sid, pid))

    def submit(self, sid, pid, header):
        with self.store.connect() as db:
            row = self._row(db, sid, pid)
            if row["status"] != "CHALLENGE_READY":
                # No resend, even if a prior HTTP call timed out or the process restarted.
                return self._public(row)
            body = json.loads(row["body"])
            self.market.agreement(sid, row["match_id"], body["binding"]["terms_hash"], db)
            mandate = db.execute("SELECT * FROM payment_mandates WHERE id=?", (row["mandate_id"],)).fetchone()
            if mandate["expires"] <= self.store.clock():
                raise MachineError("payment mandate expired")
            profile = self._profile(body)
            payload = validate_signature(header, body, self.store.clock())
            # Commit before transmitting the bearer authorization; do not persist its plaintext.
            db.execute("UPDATE payments SET status='SUBMITTED',signature_hash=?,updated=? WHERE id=?", (
                digest(payload), self.store.clock(), pid))
            self.store._event(db, pid + ":submitted", "PAYMENT_AUTHORIZATION_SENT", {
                "payment_id": pid, "authorization_hash": digest(body["authorization"]), "payload_hash": digest(payload)})
        try:
            status, headers, content = self.transport.call(profile["url"], body["request"], {
                "PAYMENT-SIGNATURE": header, "Idempotency-Key": pid})
            claimed = inspect_response(headers.get("payment-response"), PaymentBinding(**body["binding"]))
            delivery = None
            if 200 <= status < 300:
                artifact = json.loads(content)
                require_keys(artifact, {"terms_hash", "data_version", "data"}, "resource delivery")
                if artifact["terms_hash"] == body["binding"]["terms_hash"] and artifact["data_version"] == body["request"]["data_version"]:
                    delivery = {"assurance": "TERMS_AND_VERSION_BINDING_NOT_DATA_TRUTH", "artifact": artifact,
                                "artifact_hash": digest(artifact)}
            with self.store.connect() as db:
                current = self._row(db, sid, pid)
                if current["status"] == "PAID_DELIVERY_MISSING" and delivery:
                    db.execute("UPDATE payments SET status='SETTLED',delivery=?,updated=? WHERE id=?", (
                        canonical(delivery).decode(), self.store.clock(), pid))
                else:
                    db.execute("UPDATE payments SET status='SETTLEMENT_REPORTED',tx_hash=?,delivery=?,updated=? WHERE id=? "
                        "AND status IN ('SUBMITTED','SETTLEMENT_REPORTED','UNKNOWN')", (
                    claimed["tx_hash"], canonical(delivery).decode() if delivery else None, self.store.clock(), pid))
                self.store._event(db, pid + ":reported", "PAYMENT_SETTLEMENT_REPORTED", {
                    "payment_id": pid, "tx_hash": claimed["tx_hash"], "delivery_received": delivery is not None})
        except Exception:  # noqa: BLE001 - every ambiguous transmission must retain its hold; never log signatures.
            # Narrow public reason: exceptions can contain sensitive authorization headers.
            with self.store.connect() as db:
                db.execute("UPDATE payments SET status='UNKNOWN',reason='RECONCILIATION_REQUIRED',updated=? WHERE id=? "
                    "AND status IN ('SUBMITTED','SETTLEMENT_REPORTED','UNKNOWN')", (self.store.clock(), pid))
                self.store._event(db, pid + ":unknown", "PAYMENT_OUTCOME_UNKNOWN", {"payment_id": pid})
        return self.reconcile(sid, pid)

    def reconcile(self, sid, pid):
        with self.store.connect() as db:
            row = self._row(db, sid, pid)
            if row["status"] in {"SETTLED", "PAID_DELIVERY_MISSING", "EXPIRED_UNPAID", "CANCELLED"}:
                return self._public(row)
            if row["status"] not in {"SUBMITTED", "SETTLEMENT_REPORTED", "UNKNOWN"}:
                raise MachineError("submitted payment required for chain reconciliation")
            body = json.loads(row["body"])
            profile = self._profile(body)
        try:
            observation = self.chain.observe(profile, body["authorization"], row["tx_hash"], body["start_block"])
        except Exception:  # noqa: BLE001 - untrusted RPC failures never authorize release or retransmission.
            with self.store.connect() as db:
                db.execute("UPDATE payments SET updated=?,reason='CHAIN_OBSERVATION_UNAVAILABLE' WHERE id=?", (
                    self.store.clock(), pid))
            return self.get(sid, pid)  # Retain the hold; never translate an unavailable RPC into nonpayment.
        with self.store.connect() as db:
            row = self._row(db, sid, pid)
            if row["status"] in {"SETTLED", "PAID_DELIVERY_MISSING", "EXPIRED_UNPAID", "CANCELLED"}:
                return self._public(row)
            state = row["status"]
            amount = body["binding"]["amount_atoms"]
            if observation["status"] == "PAID":
                state = "SETTLED" if row["delivery"] else "PAID_DELIVERY_MISSING"
                db.execute("UPDATE payment_mandates SET reserved=reserved-?,spent=spent+? WHERE id=?", (amount, amount, row["mandate_id"]))
            elif observation["status"] == "EXPIRED_UNPAID":
                state = "EXPIRED_UNPAID"
                db.execute("UPDATE payment_mandates SET reserved=reserved-? WHERE id=?", (amount, row["mandate_id"]))
            db.execute("UPDATE payments SET status=?,tx_hash=?,observation=?,reason=?,updated=? WHERE id=?", (
                state, observation.get("tx_hash"), canonical(observation).decode(), observation.get("reason"), self.store.clock(), pid))
            self.store._event(db, pid + ":observation:" + digest(observation), "PAYMENT_CHAIN_OBSERVED", {
                "payment_id": pid, "state": state, "observation": observation})
            return self._public(self._row(db, sid, pid))

    def cancel_unsigned(self, sid, pid):
        with self.store.connect() as db:
            row = self._row(db, sid, pid)
            if row["status"] == "CANCELLED":
                return self._public(row)
            if row["status"] not in {"PREPARED", "CHALLENGE_READY"}:
                raise MachineError("a submitted payment must be reconciled, never cancelled blindly")
            body = json.loads(row["body"])
            db.execute("UPDATE payment_mandates SET reserved=reserved-? WHERE id=?", (body["binding"]["amount_atoms"], row["mandate_id"]))
            db.execute("UPDATE payments SET status='CANCELLED',updated=? WHERE id=?", (self.store.clock(), pid))
            self.store._event(db, pid + ":cancelled", "UNSENT_PAYMENT_CANCELLED", {"payment_id": pid})
            return self._public(self._row(db, sid, pid))

    def recover(self):
        with self.store.connect() as db:
            unsigned = [(r["owner"], r["id"]) for r in db.execute("SELECT * FROM payments WHERE status IN "
                "('PREPARED','CHALLENGE_READY') AND json_extract(body,'$.binding.expires')<=? LIMIT 10", (self.store.clock(),))]
            pending = [(r["owner"], r["id"]) for r in db.execute("SELECT * FROM payments WHERE status IN "
                "('SUBMITTED','SETTLEMENT_REPORTED','UNKNOWN') ORDER BY updated LIMIT 10")]
        for sid, pid in unsigned:
            self.cancel_unsigned(sid, pid)
        failures = 0
        for sid, pid in pending:
            try:
                result = self.reconcile(sid, pid)
                failures += result.get("reason") == "CHAIN_OBSERVATION_UNAVAILABLE"
            except MachineError:
                # A changed resource profile keeps its hold without starving unrelated records.
                with self.store.connect() as db:
                    db.execute("UPDATE payments SET reason='PAYMENT_REVIEW_REQUIRED',updated=? WHERE id=?", (
                        self.store.clock(), pid))
                    self.store._event(db, pid + ":review", "PAYMENT_RECOVERY_HELD", {"payment_id": pid})
                failures += 1
        return {"inspected": len(pending), "failures": failures}
