"""Honest paired outcome and provider-usage accounting for controlled studies.

Unknown billing stays unknown. A valid but late result is not a timely agreement.
These helpers do not approve payments or extrapolate to customer conversion.
"""

import json
import math
import re
from decimal import Decimal, InvalidOperation


def final_json(content):
    if not isinstance(content, str):
        raise TypeError("missing final content")
    # Some OpenAI-compatible Qwen servers leave the closing reasoning delimiter.
    # Never use reasoning text as a tool command, or persist it in the study.
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    content = content.strip()
    if content.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
        if not match:
            raise ValueError("invalid JSON fence")
        content = match.group(1)
    value = json.loads(content)
    if not isinstance(value, dict):
        raise TypeError("JSON object required")
    return value


def usage_record(raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {key: raw.get(key) if type(raw.get(key)) is int and raw[key] >= 0 else None
              for key in ["prompt_tokens", "completion_tokens", "total_tokens"]}
    if (all(value is not None for value in result.values())
            and result["total_tokens"] != result["prompt_tokens"] + result["completion_tokens"]):
        result = dict.fromkeys(result)
    try:
        cost = Decimal(str(raw["cost"]))
        result["cost_usd"] = str(cost) if cost.is_finite() and cost >= 0 else None
    except (KeyError, InvalidOperation, ValueError):
        result["cost_usd"] = None
    return result


def usage_sum(calls):
    result = {"calls": len(calls)}
    for key in ["prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"]:
        values = [call["usage"].get(key) for call in calls]
        if any(value is None for value in values):
            result[key] = None
        elif key == "cost_usd":
            result[key] = str(sum((Decimal(v) for v in values), Decimal(0)))
        else:
            result[key] = sum(values)
    result["complete_token_accounting"] = result["total_tokens"] is not None
    result["complete_provider_cost_accounting"] = result["cost_usd"] is not None
    return result


def saving(baseline, machine):
    if baseline is None or machine is None or Decimal(str(baseline)) <= 0:
        return None
    return float(1 - Decimal(str(machine)) / Decimal(str(baseline)))


def wilson(successes, count):
    if not count:
        return None
    z = 1.959963984540054
    p = successes / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return [max(0, center - radius), min(1, center + radius)]


def paired_outcomes(pairs):
    """Exact two-sided McNemar test; pairs are (baseline success, machine success).

    Normalized independent observations are required for a population inference;
    repeated fixture categories in this study make p values descriptive only.
    """
    wins = sum(not before and after for before, after in pairs)
    losses = sum(before and not after for before, after in pairs)
    discordant = wins + losses
    p = (min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(wins, losses) + 1))
             / 2 ** discordant) if discordant else 1.0)
    return {"pairs": len(pairs), "machine_only_success": wins, "baseline_only_success": losses,
            "rate_difference": (wins - losses) / len(pairs) if pairs else None,
            "exact_mcnemar_two_sided_p": p,
            "inference_limit": "fixed repeated fixture book; descriptive, not customer-population inference"}


def timely(valid, elapsed_ms, deadline_seconds):
    return bool(valid and elapsed_ms <= deadline_seconds * 1000)
