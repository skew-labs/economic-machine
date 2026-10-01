"""Exact values and canonical hashes shared by compiler, kernel and receipts."""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any


class MachineError(ValueError):
    """An IR, state or transition cannot be safely interpreted."""


def decimal(value: Any, *, signed: bool = False) -> Decimal:
    pattern = r"-?(0|[1-9][0-9]*)(\.[0-9]+)?" if signed else r"(0|[1-9][0-9]*)(\.[0-9]+)?"
    if not isinstance(value, str) or len(value) > 80 or re.fullmatch(pattern, value) is None:
        raise MachineError("amount must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise MachineError("invalid decimal") from exc
    if not result.is_finite() or (not signed and result < 0):
        raise MachineError("amount must be finite and nonnegative")
    return result


def decstr(value: Decimal) -> str:
    if not value.is_finite():
        raise MachineError("non-finite calculation")
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in {"", "-0"} else result


def canonical(value: Any) -> bytes:
    def check(item: Any) -> None:
        if isinstance(item, float):
            raise MachineError("binary floating point is forbidden")
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise MachineError("JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and not isinstance(item, (str, int, bool)):
            raise MachineError("unsupported canonical value")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def ident(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 80
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]*", value) is None):
        raise MachineError(f"invalid {label}")
    return value


def utc(value: Any) -> str:
    from datetime import datetime, timezone
    if not isinstance(value, str):
        raise MachineError("UTC timestamp string required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MachineError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise MachineError("timestamp must be UTC")
    return parsed.isoformat()


def require_keys(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise MachineError(f"{label} requires exactly {sorted(keys)}")
    return value
