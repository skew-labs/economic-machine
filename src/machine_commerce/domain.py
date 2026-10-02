"""Exact money, immutable product contracts, and bounded purchase requests."""

import re
from datetime import UTC, datetime
from decimal import localcontext

from economic_machine.values import MachineError, decimal, decstr, digest, require_keys

SCALE = 1_000_000
ASSET = "TEST_CREDIT"
CATALOG = {
    "apac-compute-brief": {
        "id": "apac-compute-brief", "name": "Atlas APAC Compute Brief", "provider": "Skew Atlas",
        "provider_id": "skew-atlas", "price": "0.01", "category": "Data",
        "description": "Version-bound original analysis of official APAC public compute list prices.",
        "delivery_schema": "apac-compute-brief-1", "verification": "Content root, source roots, coverage and derivation",
        "settlement": "Verified delivery; sandbox credits unless a separate x402 resource is configured", "version": 1,
    },
    "csv-normalize": {
        "id": "csv-normalize", "name": "CSV 정규화", "provider": "Format Worker",
        "provider_id": "format-worker", "price": "0.12", "category": "서비스",
        "description": "CSV를 열 순서와 숫자 형식이 고정된 JSON으로 변환합니다.",
        "delivery_schema": "normalized-csv-1", "verification": "행·열·값을 원본과 다시 대조",
        "settlement": "검증 통과 후 지급", "version": 1,
    },
    "arbitrum-state": {
        "id": "arbitrum-state", "name": "Arbitrum 상태 데이터", "provider": "Chain Observer",
        "provider_id": "chain-observer", "price": "0.04", "category": "데이터",
        "description": "Arbitrum Sepolia의 최신 블록·시간·가스 가격을 조회합니다.",
        "delivery_schema": "arbitrum-state-1", "verification": "체인 ID·블록 해시·최대 나이 확인",
        "settlement": "검증 통과 후 지급", "version": 1,
    },
}
DEFAULT_REQUESTS = {
    "apac-compute-brief": {"report_sha256": "0" * 64, "max_age_seconds": 86400, "license": "internal-use"},
    "csv-normalize": {"columns": ["asset", "amount", "currency"],
        "numeric_columns": ["amount"],
        "csv": "asset,amount,currency\nTreasury,1200.00,USDC\nResearch,340.50,USDC\n"},
    "arbitrum-state": {"chain_id": 421614, "max_age_seconds": 120},
}
TRANSITIONS = {
    "RESERVED": {"FULFILLING", "REFUNDED"},
    "FULFILLING": {"DELIVERED", "REFUNDED"},
    "DELIVERED": {"VERIFIED", "REFUNDED"},
    "VERIFIED": {"SETTLED", "REFUNDED"},
    "SETTLED": set(), "REFUNDED": set(),
}


def now_seconds():
    return int(datetime.now(UTC).timestamp())


def money_atoms(value):
    amount = decimal(value)
    # Decimal's default precision must never silently round token atoms.
    with localcontext() as ctx:
        ctx.prec = 100
        atoms = amount * SCALE
        if atoms != atoms.to_integral_value() or atoms > 1_000_000 * SCALE:
            raise MachineError("amount must fit six decimal places and the sandbox limit")
    return int(atoms)


def money_string(atoms):
    with localcontext() as ctx:
        ctx.prec = 100
        return decstr(decimal(str(atoms)) / SCALE)


def bounded_int(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise MachineError(f"invalid {label}")
    return value


def identifier(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value) is None:
        raise MachineError(f"invalid {label}")
    return value


def validate_request(offer_id, payload):
    if offer_id == "apac-compute-brief":
        require_keys(payload, {"report_sha256", "max_age_seconds", "license"}, "Atlas request")
        if not isinstance(payload["report_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["report_sha256"]) or payload["report_sha256"] == "0" * 64:
            raise MachineError("IMMUTABLE_ATLAS_VERSION_REQUIRED")
        bounded_int(payload["max_age_seconds"], 300, 86400, "maximum Atlas age")
        if payload["license"] != "internal-use":
            raise MachineError("SUPPORTED_ATLAS_LICENSE_REQUIRED")
    elif offer_id == "csv-normalize":
        require_keys(payload, {"csv", "columns", "numeric_columns"}, "CSV request")
        columns, numeric = payload["columns"], payload["numeric_columns"]
        if (not isinstance(columns, list) or not 1 <= len(columns) <= 20
                or any(not isinstance(c, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", c) is None
                       for c in columns) or len(set(columns)) != len(columns)):
            raise MachineError("invalid CSV columns")
        if (not isinstance(numeric, list) or any(not isinstance(c, str) or c not in columns for c in numeric)
                or len(set(numeric)) != len(numeric)):
            raise MachineError("invalid numeric columns")
        if not isinstance(payload["csv"], str) or not 1 <= len(payload["csv"].encode()) <= 40_000:
            raise MachineError("CSV input must be between 1 and 40000 bytes")
    elif offer_id == "arbitrum-state":
        require_keys(payload, {"chain_id", "max_age_seconds"}, "Arbitrum request")
        if type(payload["chain_id"]) is not int or payload["chain_id"] != 421614:
            raise MachineError("only Arbitrum Sepolia is supported")
        bounded_int(payload["max_age_seconds"], 15, 300, "maximum data age")
    else:
        raise MachineError("unknown service")
    return payload


def normalize_policy(raw):
    require_keys(raw, {"budget", "max_order", "allowed_offers", "ttl_seconds"}, "purchase policy")
    budget, maximum = money_atoms(raw["budget"]), money_atoms(raw["max_order"])
    if not 0 < maximum <= budget <= 100 * SCALE:
        raise MachineError("order cap must be positive and within the 100-credit policy budget")
    allowed = raw["allowed_offers"]
    if (not isinstance(allowed, list) or not allowed or any(not isinstance(o, str) or o not in CATALOG for o in allowed)
            or len(set(allowed)) != len(allowed)):
        raise MachineError("policy needs unique known services")
    return {"budget_atoms": budget, "max_order_atoms": maximum,
        "allowed_offers": sorted(allowed),
        "ttl_seconds": bounded_int(raw["ttl_seconds"], 60, 86400, "policy lifetime")}


def terms_for(offer_id):
    return {**CATALOG[offer_id], "asset": ASSET, "delivery_deadline_seconds": 30,
            "verification_rule_version": "commerce-verifier-1"}


def terms_hash(offer_id):
    return digest(terms_for(offer_id))


def transition(old, new):
    if new not in TRANSITIONS.get(old, set()):
        raise MachineError(f"invalid commerce transition: {old} -> {new}")
    return new
