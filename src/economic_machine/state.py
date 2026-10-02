"""Canonical economic state and strictly sequenced StateDelta input.

This package has no collector. An adapter must supply and authenticate facts;
source hashes here provide identity, not truth or provider independence.
"""

import re
from datetime import datetime

from .values import MachineError, decimal, decstr, digest, ident, require_keys, utc

FACT_QUALITY = {"VALID", "VALID_ZERO", "MISSING", "ERROR"}


def _hash(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError(f"{label} must be a SHA-256 hex digest")
    return value


def normalize_fact(value: dict) -> dict:
    require_keys(value, {"value", "unit", "quality", "source_id",
                         "source_hash", "observed_at"}, "fact")
    ident(value["unit"], "fact unit")
    ident(value["source_id"], "fact source")
    _hash(value["source_hash"], "source_hash")
    at = utc(value["observed_at"])
    quality = value["quality"]
    if quality not in FACT_QUALITY:
        raise MachineError("invalid fact quality")
    if quality in {"VALID", "VALID_ZERO"}:
        number = decimal(value["value"], signed=True)
        if quality == "VALID_ZERO" and number != 0:
            raise MachineError("VALID_ZERO must equal zero")
        if quality == "VALID" and number == 0:
            raise MachineError("zero requires VALID_ZERO")
        normalized_value = decstr(number)
    elif value["value"] is None:
        normalized_value = None
    else:
        raise MachineError("missing/error fact cannot have a value")
    return {**value, "value": normalized_value, "observed_at": at}


def normalize_quote(value: dict) -> dict:
    require_keys(value, {"quote_id", "protocol", "asset", "amount", "fee",
                         "slippage_cost", "gas_cost", "observed_at", "expires_at",
                         "source_hash"}, "quote")
    for key in ("quote_id", "protocol", "asset"):
        ident(value[key], key)
    for key in ("amount", "fee", "slippage_cost", "gas_cost"):
        decimal(value[key])
    if decimal(value["amount"]) <= 0:
        raise MachineError("quote amount must be positive")
    observed = utc(value["observed_at"])
    expires = utc(value["expires_at"])
    if datetime.fromisoformat(expires) <= datetime.fromisoformat(observed):
        raise MachineError("quote expiry must follow observation")
    _hash(value["source_hash"], "quote source hash")
    return {**value, "observed_at": observed, "expires_at": expires,
            **{key: decstr(decimal(value[key])) for key in
               ("amount", "fee", "slippage_cost", "gas_cost")}}


def normalize_state(value: dict) -> dict:
    require_keys(value, {"owner_id", "network", "sequence", "as_of", "facts", "balances",
                         "exposures", "daily_losses", "quotes"}, "EconomicState")
    ident(value["owner_id"], "state owner")
    ident(value["network"], "network")
    if type(value["sequence"]) is not int or value["sequence"] < 0:
        raise MachineError("state sequence must be nonnegative integer")
    at = utc(value["as_of"])
    for key in ("facts", "balances", "exposures", "daily_losses", "quotes"):
        if not isinstance(value[key], dict):
            raise MachineError(key + " must be a map")
    facts = {}
    for path, fact in value["facts"].items():
        ident(path, "fact path")
        facts[path] = normalize_fact(fact)
        if datetime.fromisoformat(facts[path]["observed_at"]) > datetime.fromisoformat(at):
            raise MachineError("fact observation cannot be after state time")
    balances = {}
    for asset, amount in value["balances"].items():
        ident(asset, "balance asset")
        balances[asset] = decstr(decimal(amount))
    exposures = {}
    daily_losses = {}
    for key, target in (("exposures", exposures), ("daily_losses", daily_losses)):
        for agent, assets in value[key].items():
            ident(agent, "agent")
            if not isinstance(assets, dict):
                raise MachineError(key + " entry must be a map")
            target[agent] = {}
            for asset, amount in assets.items():
                ident(asset, "asset")
                target[agent][asset] = decstr(decimal(amount))
    quotes = {}
    for quote_id, raw in value["quotes"].items():
        ident(quote_id, "quote id")
        item = normalize_quote(raw)
        if datetime.fromisoformat(item["observed_at"]) > datetime.fromisoformat(at):
            raise MachineError("quote observation cannot be after state time")
        if item["quote_id"] != quote_id:
            raise MachineError("quote id mismatch")
        quotes[quote_id] = item
    return {"owner_id": value["owner_id"], "network": value["network"],
            "sequence": value["sequence"],
            "as_of": at, "facts": facts, "balances": balances,
            "exposures": exposures, "daily_losses": daily_losses, "quotes": quotes}


def state_root(state: dict) -> str:
    return digest(normalize_state(state))


def apply_delta(state: dict, delta: dict) -> tuple[dict, list[str]]:
    old = normalize_state(state)
    require_keys(delta, {"schema_version", "owner_id", "network", "sequence", "previous_root",
                         "as_of", "evidence_hash", "fact_updates", "quote_updates",
                         "account_updates"}, "StateDelta")
    if delta["schema_version"] != "econ-state-delta-1":
        raise MachineError("unsupported StateDelta version")
    if (delta["owner_id"] != old["owner_id"] or delta["network"] != old["network"]
            or delta["sequence"] != old["sequence"] + 1):
        raise MachineError("state delta owner/network/sequence mismatch")
    if delta["previous_root"] != digest(old):
        raise MachineError("state root mismatch")
    _hash(delta["evidence_hash"], "delta evidence hash")
    at = utc(delta["as_of"])
    if datetime.fromisoformat(at) < datetime.fromisoformat(old["as_of"]):
        raise MachineError("state time cannot move backwards")
    if not isinstance(delta["fact_updates"], dict) or not isinstance(delta["quote_updates"], dict):
        raise MachineError("delta updates must be maps")
    require_keys(delta["account_updates"], {"balances", "exposures", "daily_losses"},
                 "account updates")
    if any(not isinstance(delta["account_updates"][key], dict) for key in
           ("balances", "exposures", "daily_losses")):
        raise MachineError("account updates must be maps")
    result = {**old, "sequence": delta["sequence"], "as_of": at,
              "facts": dict(old["facts"]), "quotes": dict(old["quotes"]),
              "balances": dict(old["balances"]),
              "exposures": {agent: dict(assets) for agent, assets in old["exposures"].items()},
              "daily_losses": {agent: dict(assets) for agent, assets in old["daily_losses"].items()}}
    semantic_changes = []
    for path, raw in delta["fact_updates"].items():
        ident(path, "fact path")
        fact = normalize_fact(raw)
        if datetime.fromisoformat(fact["observed_at"]) > datetime.fromisoformat(at):
            raise MachineError("future observation")
        previous = old["facts"].get(path)
        if previous and datetime.fromisoformat(fact["observed_at"]) < datetime.fromisoformat(previous["observed_at"]):
            raise MachineError("out-of-order fact update requires explicit revision support")
        if previous is None or (previous["value"], previous["unit"], previous["quality"]) != (
                fact["value"], fact["unit"], fact["quality"]):
            semantic_changes.append(path)
        result["facts"][path] = fact
    for quote_id, raw in delta["quote_updates"].items():
        quote = normalize_quote(raw)
        if quote_id != quote["quote_id"]:
            raise MachineError("quote id mismatch")
        old_quote = old["quotes"].get(quote_id)
        if old_quote != quote:
            semantic_changes.append("quote:" + quote_id)
        result["quotes"][quote_id] = quote
    for asset, raw in delta["account_updates"]["balances"].items():
        ident(asset, "balance asset")
        value = decstr(decimal(raw))
        if old["balances"].get(asset) != value:
            semantic_changes.append("balance:" + asset)
        result["balances"][asset] = value
    for field, prefix in (("exposures", "exposure"), ("daily_losses", "daily_loss")):
        for agent, assets in delta["account_updates"][field].items():
            ident(agent, "agent")
            if not isinstance(assets, dict):
                raise MachineError("agent account updates must be maps")
            result[field].setdefault(agent, {})
            for asset, raw in assets.items():
                ident(asset, "asset")
                value = decstr(decimal(raw))
                if old[field].get(agent, {}).get(asset) != value:
                    semantic_changes.append(f"{prefix}:{agent}:{asset}")
                result[field][agent][asset] = value
    return normalize_state(result), sorted(semantic_changes)
