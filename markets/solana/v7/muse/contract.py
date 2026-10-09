"""Offline request assembly and untrusted-output validation; never signs or calls APIs."""
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

MODEL = "muse-spark-1.3-contributor"
NOTICE_VERSION = "contributor-public-market-2026-10-07-v1"
DATA_SCOPE = "public-market-v1"
ROOT = Path(__file__).resolve().parent
FIELDS = {
    "frame_id", "mode", "target_inventory_lots", "half_spread_ticks", "alpha_ticks",
    "inventory_skew_ticks", "clip_lots", "levels_per_side", "reprice_threshold_ticks",
    "ttl_ms", "reason_code",
}
REASONS = {"BALANCED", "INVENTORY", "VOLATILITY", "ADVERSE_SELECTION", "NO_EDGE", "BAD_DATA", "RISK_LIMIT", "RECONCILING"}
PUBLIC_MARKETS = frozenset({"SOL-PERP-EXAMPLE", "BTC-PERP-EXAMPLE"})
PUBLIC_FEATURE_RANGES = {
    "spread_ticks": (0, (1 << 24) - 1),
    "imbalance_bps": (-10000, 10000),
    "microprice_offset_ticks": (-(1 << 24) + 1, (1 << 24) - 1),
    "volatility_bps": (0, 100000),
    "depth_lots_l1": (0, 1_000_000_000),
    "depth_lots_l4": (0, 1_000_000_000),
    "depth_lots_l16": (0, 1_000_000_000),
    "oracle_age_ms": (0, 1000),
}
# Published template constants, never copied from an owner's mandate or inventory.
PUBLIC_BOUNDS = {
    "allowed_modes": ["QUOTE", "HOLD", "HALT"],
    "target_inventory_min_lots": 0, "target_inventory_max_lots": 0,
    "max_clip_lots": 2, "max_levels_per_side": 2, "max_half_spread_ticks": 20,
    "max_abs_alpha_ticks": 2, "max_inventory_skew_ticks": 0,
    "max_reprice_threshold_ticks": 4, "max_ttl_ms": 5000,
}


@dataclass(frozen=True)
class ActivationState:
    """Offline view of a trusted server-side record, not a customer consent token.

    Live authentication, persistent grants, eligibility and source-rights review are
    not implemented. Defaults prohibit request preparation; tests use synthetic grants.
    """
    enabled: bool = False
    training_acknowledged: bool = False
    notice_version: str = ""
    data_scope: str = ""
    eligible: bool = False
    source_use_approved: bool = False
    expires_ns: int = 0
    revoked: bool = False


class Rejected(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("DUPLICATE_KEY")
        result[key] = value
    return result


def integer(value, lo=-(1 << 63), hi=(1 << 63) - 1):
    if type(value) is not int or not lo <= value <= hi:
        raise Rejected("INTEGER_RANGE")
    return value


def require_activation(activation, now_ns):
    integer(now_ns, 0)
    if not isinstance(activation, ActivationState):
        raise Rejected("ACTIVATION_REQUIRED")
    if any(value is not True for value in (
        activation.enabled, activation.training_acknowledged,
        activation.eligible, activation.source_use_approved,
    )) or activation.revoked is not False:
        raise Rejected("ACTIVATION_REQUIRED")
    if activation.notice_version != NOTICE_VERSION or activation.data_scope != DATA_SCOPE:
        raise Rejected("NOTICE_OR_SCOPE_CHANGED")
    if now_ns >= integer(activation.expires_ns, 0):
        raise Rejected("ACTIVATION_EXPIRED")


def build_request(public_frame, *, activation=None, now_ns):
    """Accept a dedicated public-feed snapshot, never a full customer frame.

    Field/type restrictions reduce accidental disclosure, not covert encoding in
    numbers. Production must use a reviewed public-feed collector, not customer input.
    """
    require_activation(activation, now_ns)
    expected = {"market", "source_slot", "observed_monotonic_ns", "valid", "features"}
    if type(public_frame) is not dict or set(public_frame) != expected:
        raise Rejected("PUBLIC_FRAME_FIELDS")
    market = public_frame["market"]
    if type(market) is not str or market not in PUBLIC_MARKETS:
        raise Rejected("PUBLIC_MARKET")
    if public_frame["valid"] is not True:
        raise Rejected("INVALID_PUBLIC_FEED")
    observed = integer(public_frame["observed_monotonic_ns"], 0)
    if observed > now_ns:
        raise Rejected("FUTURE_PUBLIC_FEED")
    source_slot = integer(public_frame["source_slot"], 0)
    features = public_frame["features"]
    if type(features) is not dict or set(features) != set(PUBLIC_FEATURE_RANGES):
        raise Rejected("PUBLIC_FEATURE_FIELDS")
    projected = {key: integer(features[key], *limits) for key, limits in PUBLIC_FEATURE_RANGES.items()}
    if projected["oracle_age_ms"] * 1_000_000 + now_ns - observed > 1_000_000_000:
        raise Rejected("STALE_PUBLIC_FEED")
    frame = {"frame_id": secrets.token_hex(16), "market": market, "source_slot": source_slot,
             "features": projected, "bounds": PUBLIC_BOUNDS}
    content = json.dumps(frame, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    schema = json.loads((ROOT / "policy.schema.json").read_text())
    schema.pop("$schema", None)
    schema.pop("title", None)
    return {
        "model": MODEL,
        "reasoning_effort": "minimal",
        "max_completion_tokens": 1024,
        "stream": False,
        "prompt_cache_key": "machine-perps-contributor-public-v1",
        "messages": [
            {"role": "system", "content": (ROOT / "system.md").read_text()},
            {"role": "user", "content": content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "machine_perps_public_policy_v1", "strict": True, "schema": schema},
        },
    }


def validate_response(response, frame, *, now_ns, deadline_ns, activation=None):
    """Returns a candidate only. Native/on-chain risk and authority remain mandatory.

    frame must be the caller's immutable outstanding request, not a model-supplied echo.
    The caller must separately recheck current risk/config/fork/mandate versions before activation.
    """
    require_activation(activation, now_ns)
    integer(deadline_ns, 0)
    observed = integer(frame["observed_monotonic_ns"], 0)
    if observed > now_ns or now_ns >= deadline_ns:
        raise Rejected("DEADLINE")
    if type(response) is not dict or response.get("model") != MODEL:
        raise Rejected("MODEL_ID")
    choices = response.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise Rejected("CHOICES")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise Rejected("INCOMPLETE")
    message = choice.get("message")
    if type(message) is not dict or message.get("refusal") or message.get("tool_calls"):
        raise Rejected("NON_POLICY_RESPONSE")
    raw = message.get("content")
    if type(raw) is not str or len(raw.encode()) > 4096:
        raise Rejected("OUTPUT_SIZE")
    try:
        output = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError) as error:
        raise Rejected("INVALID_JSON") from error
    if type(output) is not dict or set(output) != FIELDS:
        raise Rejected("SCHEMA")
    if type(output["frame_id"]) is not str or output["frame_id"] != frame["frame_id"]:
        raise Rejected("FRAME_MISMATCH")
    bounds, features = frame["bounds"], frame["features"]
    mode = output["mode"]
    if type(mode) is not str or mode not in {"QUOTE", "HOLD", "HALT"} or mode not in bounds["allowed_modes"]:
        raise Rejected("MODE")
    if type(output["reason_code"]) is not str or output["reason_code"] not in REASONS:
        raise Rejected("REASON")
    numbers = FIELDS - {"frame_id", "mode", "reason_code"}
    for field in numbers:
        integer(output[field])
    if output["target_inventory_lots"] != 0 or output["inventory_skew_ticks"] != 0:
        raise Rejected("PRIVATE_POLICY_FIELD")
    target = output["target_inventory_lots"]
    if mode in {"QUOTE", "REDUCE"}:
        integer(target, integer(bounds["target_inventory_min_lots"]), integer(bounds["target_inventory_max_lots"]))
    for field, bound in (
        ("half_spread_ticks", "max_half_spread_ticks"),
        ("inventory_skew_ticks", "max_inventory_skew_ticks"),
        ("clip_lots", "max_clip_lots"),
        ("levels_per_side", "max_levels_per_side"),
        ("reprice_threshold_ticks", "max_reprice_threshold_ticks"),
    ):
        integer(output[field], 0, min(integer(bounds[bound], 0), PUBLIC_BOUNDS[bound]))
    integer(output["levels_per_side"], 0, 8)
    alpha_bound = min(integer(bounds["max_abs_alpha_ticks"], 0), PUBLIC_BOUNDS["max_abs_alpha_ticks"])
    integer(output["alpha_ticks"], -alpha_bound, alpha_bound)
    ttl = integer(output["ttl_ms"], 1, min(integer(bounds["max_ttl_ms"], 1), PUBLIC_BOUNDS["max_ttl_ms"]))
    expires = observed + ttl * 1_000_000
    if expires > (1 << 63) - 1 or now_ns >= expires:
        raise Rejected("EXPIRED_INPUT")
    if mode in {"QUOTE", "REDUCE"}:
        if frame.get("valid") is not True or frame.get("reconciled") is not True:
            raise Rejected("UNHEALTHY_FRAME")
        age_ms = integer(features["oracle_age_ms"], 0)
        max_age_ms = integer(bounds["max_oracle_age_ms"], 0)
        # Freshness at response time, not only at request time.
        if age_ms * 1_000_000 + now_ns - observed > max_age_ms * 1_000_000:
            raise Rejected("STALE_ORACLE")
    if mode in {"HOLD", "HALT"}:
        if any(output[f] for f in numbers - {"ttl_ms"}):
            raise Rejected("NONZERO_STOP_POLICY")
    else:
        if min(output["half_spread_ticks"], output["clip_lots"], output["levels_per_side"]) <= 0:
            raise Rejected("QUOTE_SHAPE")
        position = integer(features["position_lots"])
        depth = output["clip_lots"] * output["levels_per_side"]
        max_position = integer(bounds["max_abs_position_lots"], 0)
        if max(abs(position + depth), abs(position - depth)) > max_position:
            raise Rejected("OPEN_ORDER_EXPOSURE")
    return {"candidate": output, "valid_until_monotonic_ns": min(expires, deadline_ns), "execution_authority": False}
