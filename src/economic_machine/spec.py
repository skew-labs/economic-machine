"""Versioned source of truth for the typed Economic ISA.

This describes syntax and phase movement, not financial truth or execution
authority. A semantic change requires a new ISA version and conformance set.
"""

from .values import MachineError, digest

ISA_V1 = "econ-isa-1"
ISA_V2 = "econ-isa-2"
ISA_V3 = "econ-isa-3"
ALL_VERSIONS = [ISA_V1, ISA_V2, ISA_V3]


def _op(phase: str, order: int, fields: list[str],
        versions: list[str] | None = None) -> dict:
    return {"status": "implemented", "phase": phase, "order": order,
            "fields": ["op", *fields],
            "versions": list(versions if versions is not None else ALL_VERSIONS)}


SPEC = {
    "schema_version": "economic-isa-spec-1",
    "versions": list(ALL_VERSIONS),
    "limits": {"instructions": 64, "branches": 8, "paths": 256},
    "opcodes": {
        "OBSERVE": _op("OBSERVING", 0, ["path", "as", "max_age_ms"]),
        "PRICE": _op("OBSERVING", 0, ["paths", "as", "max_age_ms", "max_dispersion_bps"]),
        "ASSERT": _op("ASSERTING", 1, ["register", "comparison", "value", "unit"]),
        "GUARD": _op("ASSERTING", 1, ["register", "comparison", "value", "unit",
                                     "on_false", "reason"], [ISA_V2]),
        "BRANCH": _op("ASSERTING", 1, ["register", "comparison", "value", "unit",
                                      "if_true", "if_false"], [ISA_V3]),
        "QUOTE": _op("QUOTED", 2, ["asset", "amount", "max_age_ms"]),
        "SCORE": _op("SCORED", 3, ["objective", "as"]),
        "ALLOCATE": _op("ALLOCATED", 4, ["asset", "amount", "route_register"]),
        "SIMULATE": _op("SIMULATED", 5, []),
        "VERIFY": _op("VERIFIED", 6, []),
        "PREPARE": _op("PREPARED", 7, []),
        "SETTLE": _op("AWAITING_AUTHORIZATION", 8, []),
        "ESCALATE": _op("ESCALATED", 8, ["reason"]),
        "ABORT": _op("ABORTED", 8, ["reason"]),
        "HOLD": _op("HELD", 8, ["reason"], [ISA_V3]),
        "HEDGE": {"status": "reserved", "phase": None, "order": None,
                  "fields": [], "versions": []},
        "SWAP": {"status": "reserved", "phase": None, "order": None,
                 "fields": [], "versions": []},
        "BORROW": {"status": "reserved", "phase": None, "order": None,
                   "fields": [], "versions": []},
        "REPAY": {"status": "reserved", "phase": None, "order": None,
                  "fields": [], "versions": []},
        "CANCEL": {"status": "reserved", "phase": None, "order": None,
                   "fields": [], "versions": []},
    },
    "forward": {
        "CREATED": ["OBSERVING", "QUOTED", "ESCALATED", "ABORTED", "HELD"],
        "OBSERVING": ["OBSERVING", "ASSERTING", "QUOTED", "ESCALATED", "ABORTED", "HELD"],
        "ASSERTING": ["ASSERTING", "QUOTED", "ESCALATED", "ABORTED", "HELD"],
        "QUOTED": ["SCORED", "ESCALATED", "ABORTED", "HELD"],
        "SCORED": ["ALLOCATED", "ESCALATED", "ABORTED", "HELD"],
        "ALLOCATED": ["SIMULATED", "ESCALATED", "ABORTED", "HELD"],
        "SIMULATED": ["VERIFIED", "ESCALATED", "ABORTED", "HELD"],
        "VERIFIED": ["PREPARED", "ESCALATED", "ABORTED", "HELD"],
        "PREPARED": ["AWAITING_AUTHORIZATION", "ESCALATED", "ABORTED"],
        "AWAITING_AUTHORIZATION": [], "ESCALATED": [], "ABORTED": [], "HELD": [],
    },
    "terminal_opcodes": ["SETTLE", "ESCALATE", "ABORT", "HOLD"],
    "early_terminal_opcodes": ["ESCALATE", "ABORT", "HOLD"],
    "normal_path_required": ["QUOTE", "SCORE", "ALLOCATE", "SIMULATE",
                             "VERIFY", "PREPARE", "SETTLE"],
    "halt_phases": ["ESCALATED", "ABORTED", "HELD"],
}


def validate_spec(spec: dict) -> None:
    """Fail startup on an incomplete or internally inconsistent ISA table."""
    if spec["schema_version"] != "economic-isa-spec-1":
        raise MachineError("unsupported ISA spec")
    if spec["versions"] != ALL_VERSIONS:
        raise MachineError("ISA spec version list changed")
    phases = set(spec["forward"])
    if "CREATED" not in phases or any(len(set(targets)) != len(targets)
                                       or not set(targets) <= phases
                                       for targets in spec["forward"].values()):
        raise MachineError("invalid phase graph")
    if any(type(spec["limits"][key]) is not int or spec["limits"][key] < 1
           for key in ("instructions", "branches", "paths")):
        raise MachineError("invalid ISA limits")
    implemented = {key for key, value in spec["opcodes"].items()
                   if value["status"] == "implemented"}
    reserved = set(spec["opcodes"]) - implemented
    if not implemented or not reserved:
        raise MachineError("ISA needs implemented and reserved opcodes")
    for name, value in spec["opcodes"].items():
        if value["status"] == "reserved":
            if value != {"status": "reserved", "phase": None, "order": None,
                          "fields": [], "versions": []}:
                raise MachineError("reserved opcode has executable metadata: " + name)
            continue
        if (value["status"] != "implemented" or value["phase"] not in phases
                or type(value["order"]) is not int or not 0 <= value["order"] <= 8
                or not value["versions"] or not set(value["versions"]) <= set(ALL_VERSIONS)
                or not value["fields"] or value["fields"][0] != "op"
                or len(set(value["fields"])) != len(value["fields"])):
            raise MachineError("invalid opcode specification: " + name)
    for key in ("terminal_opcodes", "early_terminal_opcodes", "normal_path_required"):
        if not set(spec[key]) <= implemented:
            raise MachineError("path rule names unknown opcode")
    if not set(spec["halt_phases"]) <= phases:
        raise MachineError("halt rule names unknown phase")
    if not set(spec["early_terminal_opcodes"]) <= set(spec["terminal_opcodes"]):
        raise MachineError("early terminal is not terminal")
    digest(spec)  # Reject floats, custom objects and noncanonical values.


validate_spec(SPEC)
SPEC_HASH = digest(SPEC)


def conformance_vectors() -> list[dict]:
    """Portable transition corpus for another runtime (for example Rust)."""
    vectors = []
    for phase in sorted(SPEC["forward"]):
        for op, rule in sorted(SPEC["opcodes"].items()):
            if rule["status"] == "implemented":
                target = rule["phase"]
                vectors.append({"phase": phase, "opcode": op,
                                "next_phase": target if target in SPEC["forward"][phase] else None})
    return vectors
