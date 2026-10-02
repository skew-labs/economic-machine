"""Untrusted inference boundary for the deterministic Economic Machine.

A model may suggest a typed EconomicProgram or report ambiguity. It cannot
register a program, write an EconomicState, reserve capital, or authorize a
transaction. The caller-provided scope is a local review boundary, not a
signature or proof of what the user said.
"""

import json
import re
from datetime import datetime

from .compiler import _check_sandbox, compile_program
from .state import normalize_state, state_root
from .values import MachineError, canonical, decimal, decstr, digest, ident, require_keys, utc

SCOPE_VERSION = "economic-inference-scope-1"
DRAFT_VERSION = "economic-inference-draft-1"
ASSESSMENT_VERSION = "economic-inference-assessment-1"
OPINION_VERSION = "model-opinion-1"
MAX_SCOPE_BYTES = 8192
MAX_DRAFT_BYTES = 65536


def _hex_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError(label + " must be a SHA-256 hex digest")
    return value


def _bounded_json(value: dict, maximum: int, label: str) -> dict:
    encoded = canonical(value)
    if len(encoded) > maximum:
        raise MachineError(label + " exceeds its size limit")
    return json.loads(encoded)


def normalize_inference_scope(value: dict) -> dict:
    """Validate a local review scope without treating it as user authorization."""
    require_keys(value, {"schema_version", "request_hash", "state_root", "program_id",
                         "owner_id", "agent_id", "network", "sandbox",
                         "allowed_fact_paths"}, "InferenceScope")
    if value["schema_version"] != SCOPE_VERSION:
        raise MachineError("unsupported inference scope version")
    for key in ("request_hash", "state_root"):
        _hex_digest(value[key], key)
    for key in ("program_id", "owner_id", "agent_id", "network"):
        ident(value[key], key)
    paths = value["allowed_fact_paths"]
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 64
            or any(not isinstance(path, str) for path in paths)):
        raise MachineError("allowed fact paths must be a bounded list")
    for path in paths:
        ident(path, "allowed fact path")
    if len(set(paths)) != len(paths):
        raise MachineError("duplicate allowed fact path")
    _check_sandbox(value["sandbox"])
    return _bounded_json(value, MAX_SCOPE_BYTES, "inference scope")


def normalize_inference_draft(value: dict) -> dict:
    """Validate the transport envelope, including unused model output."""
    require_keys(value, {"schema_version", "model_id", "model_hash", "request_hash",
                         "state_root", "created_at", "expires_at", "confidence",
                         "ambiguities", "program"}, "InferenceDraft")
    if value["schema_version"] != DRAFT_VERSION:
        raise MachineError("unsupported inference draft version")
    ident(value["model_id"], "model id")
    for key in ("model_hash", "request_hash", "state_root"):
        _hex_digest(value[key], key)
    created_at = utc(value["created_at"])
    expires_at = utc(value["expires_at"])
    if datetime.fromisoformat(expires_at) <= datetime.fromisoformat(created_at):
        raise MachineError("inference expiry must follow creation")
    confidence = decimal(value["confidence"])
    if confidence > 1:
        raise MachineError("inference confidence must be between zero and one")
    ambiguities = value["ambiguities"]
    if (not isinstance(ambiguities, list) or len(ambiguities) > 16
            or any(not isinstance(item, str) for item in ambiguities)):
        raise MachineError("ambiguities must be a bounded list of reason codes")
    for item in ambiguities:
        ident(item, "ambiguity reason")
    if len(set(ambiguities)) != len(ambiguities):
        raise MachineError("duplicate ambiguity reason")
    if value["program"] is not None and not isinstance(value["program"], dict):
        raise MachineError("inferred program must be an object or null")
    return _bounded_json({**value, "created_at": created_at, "expires_at": expires_at},
                         MAX_DRAFT_BYTES, "inference draft")


def _within_scope(program: dict, scope: dict) -> bool:
    for key in ("program_id", "owner_id", "agent_id", "network"):
        if program[key] != scope[key]:
            return False
    proposed = program["sandbox"]
    allowed = scope["sandbox"]
    if proposed["asset"] != allowed["asset"]:
        return False
    if not set(proposed["allowed_protocols"]).issubset(allowed["allowed_protocols"]):
        return False
    if datetime.fromisoformat(proposed["expires_at"]) > datetime.fromisoformat(allowed["expires_at"]):
        return False
    for key in ("capital_limit", "max_exposure", "max_total_cost", "max_daily_loss"):
        if decimal(proposed[key]) > decimal(allowed[key]):
            return False
    for ins in program["instructions"]:
        if ins["op"] == "OBSERVE" and ins["path"] not in scope["allowed_fact_paths"]:
            return False
        if (ins["op"] == "PRICE"
                and not set(ins["paths"]).issubset(scope["allowed_fact_paths"])):
            return False
        if (ins["op"] in {"QUOTE", "ALLOCATE"}
                and decimal(ins["amount"]) > decimal(allowed["capital_limit"])):
            return False
    return True


def assess_inference(draft: dict, scope: dict, world: dict, *, at: str) -> dict:
    """Return a non-executable decision about an LLM-produced proposal.

    A valid proposal is REVIEW_REQUIRED, never approved. A missing answer is
    ABSTAIN. An invalid/out-of-scope proposal is REJECTED. No model call occurs.
    """
    scope = normalize_inference_scope(scope)
    draft = normalize_inference_draft(draft)
    state = normalize_state(world)
    at = utc(at)
    if scope["owner_id"] != state["owner_id"] or scope["network"] != state["network"]:
        raise MachineError("inference scope owner/network mismatch")
    if scope["state_root"] != state_root(state):
        raise MachineError("inference scope is for a different state")
    if datetime.fromisoformat(at) < datetime.fromisoformat(state["as_of"]):
        raise MachineError("assessment time precedes state")
    if datetime.fromisoformat(at) >= datetime.fromisoformat(scope["sandbox"]["expires_at"]):
        raise MachineError("inference scope expired")
    reasons: list[str] = []
    compiled_hash = None
    if (draft["request_hash"] != scope["request_hash"]
            or draft["state_root"] != scope["state_root"]):
        status, reasons = "REJECTED", ["CONTEXT_MISMATCH"]
    elif datetime.fromisoformat(draft["created_at"]) > datetime.fromisoformat(at):
        status, reasons = "REJECTED", ["FUTURE_INFERENCE"]
    elif datetime.fromisoformat(at) >= datetime.fromisoformat(draft["expires_at"]):
        status, reasons = "ABSTAIN", ["STALE_INFERENCE"]
    elif draft["ambiguities"]:
        status, reasons = "ABSTAIN", ["MISSING_INFORMATION", *draft["ambiguities"]]
    elif draft["program"] is None:
        status, reasons = "ABSTAIN", ["NO_PROGRAM_PROPOSED"]
    else:
        try:
            compiled = compile_program(draft["program"])
        except MachineError:
            status, reasons = "REJECTED", ["COMPILE_REJECTED"]
        else:
            if _within_scope(compiled, scope):
                status = "REVIEW_REQUIRED"
                compiled_hash = compiled["program_hash"]
            else:
                status, reasons = "REJECTED", ["OUTSIDE_SCOPE"]
    result = {"schema_version": ASSESSMENT_VERSION, "status": status,
              "reason_codes": reasons, "candidate_hash": digest(draft),
              "scope_hash": digest(scope), "state_root": state_root(state),
              "state_sequence": state["sequence"], "at": at,
              "program_hash": compiled_hash, "model_id": draft["model_id"],
              "model_hash": draft["model_hash"],
              "confidence": draft["confidence"],
              "registration_status": "NOT_REGISTERED",
              "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
    result["assessment_hash"] = digest(result)
    return result


def normalize_model_opinion(value: dict) -> dict:
    """Validate a bounded prediction plugin result with no policy authority."""
    require_keys(value, {"schema_version", "mode", "model_id", "model_revision",
        "model_hash", "input_root", "target", "horizon_seconds", "scores",
        "uncertainty_bps", "valid_from", "valid_until", "reason_codes"},
        "ModelOpinionV1")
    if value["schema_version"] != OPINION_VERSION:
        raise MachineError("unsupported model opinion version")
    if value["mode"] not in {"SHADOW", "NOT_USED"}:
        raise MachineError("model opinion cannot enter the execution path")
    for key in ("model_id", "model_revision", "target"):
        ident(value[key], key)
    for key in ("model_hash", "input_root"):
        _hex_digest(value[key], key)
    horizon = value["horizon_seconds"]
    if type(horizon) is not int or not 1 <= horizon <= 365 * 86400:
        raise MachineError("model opinion horizon outside bound")
    scores = require_keys(value["scores"], {"expected_return_bps", "risk_bps",
                                            "liquidity_bps"}, "model scores")
    normalized_scores = {}
    for key, score in scores.items():
        if score is None:
            normalized_scores[key] = None
        else:
            parsed = decimal(score, signed=key == "expected_return_bps")
            if (key == "expected_return_bps" and abs(parsed) > 1_000_000) or (
                    key != "expected_return_bps" and parsed > 1_000_000):
                raise MachineError("model score outside bound")
            normalized_scores[key] = decstr(parsed)
    uncertainty = value["uncertainty_bps"]
    if uncertainty is not None and (type(uncertainty) is not int
                                    or not 0 <= uncertainty <= 10000):
        raise MachineError("invalid model uncertainty")
    start, end = utc(value["valid_from"]), utc(value["valid_until"])
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MachineError("model opinion validity window is empty")
    reasons = value["reason_codes"]
    if (not isinstance(reasons, list) or not reasons or len(reasons) > 16
            or len(reasons) != len(set(reasons))):
        raise MachineError("bounded model opinion reasons required")
    for reason in reasons:
        ident(reason, "model opinion reason")
    if value["mode"] == "NOT_USED" and any(score is not None for score in normalized_scores.values()):
        raise MachineError("unused opinion cannot invent neutral scores")
    return _bounded_json({**value, "scores": normalized_scores,
                          "valid_from": start, "valid_until": end},
                         MAX_DRAFT_BYTES, "model opinion")


def deterministic_baseline_opinion(input_root: str, target: str,
                                   horizon_seconds: int, *, valid_from: str,
                                   valid_until: str) -> dict:
    """Explicit abstention baseline; missing risk is null, never a zero-risk claim."""
    model_hash = digest({"domain": "deterministic-model-opinion-baseline-1",
                         "behavior": "ABSTAIN"})
    return normalize_model_opinion({"schema_version": OPINION_VERSION,
        "mode": "NOT_USED", "model_id": "deterministic-baseline",
        "model_revision": "v1", "model_hash": model_hash,
        "input_root": input_root, "target": target,
        "horizon_seconds": horizon_seconds,
        "scores": {"expected_return_bps": None, "risk_bps": None,
                   "liquidity_bps": None}, "uncertainty_bps": None,
        "valid_from": valid_from, "valid_until": valid_until,
        "reason_codes": ["NO_PREDICTIVE_MODEL_USED"]})


def bind_model_opinion(value: dict, *, expected_input_root: str, at: str) -> dict:
    """Bind a shadow/unused opinion to input and time without consuming it."""
    opinion = normalize_model_opinion(value)
    expected = _hex_digest(expected_input_root, "expected input root")
    moment = utc(at)
    reasons = []
    if opinion["input_root"] != expected:
        reasons.append("INPUT_ROOT_MISMATCH")
    if not (datetime.fromisoformat(opinion["valid_from"])
            <= datetime.fromisoformat(moment)
            < datetime.fromisoformat(opinion["valid_until"])):
        reasons.append("OUTSIDE_VALIDITY")
    status = "REJECTED" if reasons else opinion["mode"]
    result = {"schema_version": "model-opinion-binding-1", "status": status,
        "reason_codes": reasons or opinion["reason_codes"],
        "opinion_hash": digest(opinion), "input_root": expected, "at": moment,
        "optimizer_effect": "NONE", "execution_authority": "NONE",
        "chain_status": "NOT_SUBMITTED"}
    result["binding_hash"] = digest(result)
    return result
