"""Finite lifecycle for one economic decision, separate from economic state."""

from .spec import SPEC
from .values import MachineError

PHASE_FOR_OPCODE = {name: rule["phase"] for name, rule in SPEC["opcodes"].items()
                    if rule["status"] == "implemented"}
FORWARD = {phase: set(targets) for phase, targets in SPEC["forward"].items()}


def transition(phase: str, opcode: str) -> str:
    next_phase = PHASE_FOR_OPCODE.get(opcode)
    if next_phase is None or next_phase not in FORWARD.get(phase, set()):
        raise MachineError(f"invalid economic transition: {phase} -> {opcode}")
    return next_phase


def halt(phase: str, terminal: str) -> str:
    if terminal not in SPEC["halt_phases"] or terminal not in FORWARD.get(phase, set()):
        raise MachineError(f"invalid halt transition: {phase} -> {terminal}")
    return terminal
