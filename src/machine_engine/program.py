"""Typed bounded native programs. Compilation/evaluation grant no authority.

The JSON format is a cold boundary. Evaluation copies at most 32 scalar fields
into a fixed frame. The C++ interpreter has no heap, loops, I/O or LLM calls.
Accounting and order approval continue to use the durable Python authority.
"""

import ctypes as c

from economic_machine.values import MachineError, digest, require_keys

from .native import NativeGate

UNITS = {name: index for index, name in enumerate(
    ["none", "scalar", "money", "quantity", "price", "rate", "duration", "boolean", "count"])}
OPS = {name: index + 1 for index, name in enumerate([
    "constant", "load", "add", "subtract", "minimum", "maximum", "multiply_scaled",
    "divide_scaled", "less", "less_equal", "equal", "logical_and", "logical_or",
    "logical_not", "select", "assert_true", "candidate", "abstain", "absolute", "negate",
    "clamp", "floor_step", "ceil_step", "notional_buy", "notional_sell", "fee_reserve"])}
CODES = ["OKAY", "MALFORMED", "UNINITIALIZED", "UNIT_MISMATCH", "OVERFLOW", "DIVIDE_ZERO",
         "ASSERTION_FAILED", "STATE_INVALID", "STALE", "EXPIRED", "EXPLICIT_ABSTENTION",
         "NO_CANDIDATE", "FUTURE_STATE"]
ACTIONS = {1: "HOLD", 2: "REDUCE", 3: "HEDGE", 4: "CANCEL", 5: "ESCALATE"}


class Instruction(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ["opcode", "destination", "a", "b", "c", "unit"]] + [("immediate", c.c_int64)]


class Program(c.Structure):
    _fields_ = [("version", c.c_uint32), ("count", c.c_uint32),
                ("field_units", c.c_uint32 * 32), ("instructions", Instruction * 128)]


FRAME_FIELDS = [("version", c.c_uint32), ("valid", c.c_uint32)] + [
    (name, c.c_uint64) for name in ["sequence", "expected_sequence", "observed_ns", "now_ns", "max_age_ns", "deadline_ns"]]


class Frame(c.Structure):
    _fields_ = FRAME_FIELDS + [("values", c.c_int64 * 32)]


class Result(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ["version", "code", "failed_instruction", "candidate"]] + [
        ("sequence", c.c_uint64), ("valid_until_ns", c.c_uint64), ("score", c.c_int64), ("execution_authority", c.c_uint64)]


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise MachineError("NATIVE_PROGRAM_INTEGER_RANGE")
    return value


def example():
    def ins(op, dst, unit, value=0, a=0, b=0):
        return {"op": op, "dst": dst, "unit": unit, "value": value, "a": a, "b": b, "c": 0}
    return {"program": {"version": 1, "fields": ["rate"], "instructions": [
        ins("load", 0, "rate"), ins("constant", 1, "rate", 800000),
        ins("less", 2, "boolean", a=0, b=1), ins("assert_true", 3, "boolean", a=2),
        ins("constant", 4, "scalar", 900000), ins("candidate", 5, "scalar", 2, a=4)]},
        "frame": {"version": 1, "valid": 1, "sequence": 7, "expected_sequence": 7,
                  "observed_ns": 900, "now_ns": 950, "max_age_ns": 100, "deadline_ns": 1100,
                  "values": [700000]}}


class NativeProgram:
    def __init__(self, gate=None):
        self.gate = gate or NativeGate()
        lib = self.gate.library
        if lib is None:
            return
        for name, structure in [("machine_program_size", Program), ("machine_frame_size", Frame),
                                ("machine_program_output_size", Result)]:
            function = getattr(lib, name)
            function.restype = c.c_size_t
            if function() != c.sizeof(structure):
                raise MachineError("NATIVE_PROGRAM_ABI_MISMATCH")
        lib.machine_program_validate.argtypes = [c.POINTER(Program), c.POINTER(c.c_uint32), c.POINTER(c.c_uint32)]
        lib.machine_program_validate.restype = c.c_int
        lib.machine_program_run.argtypes = [c.POINTER(Program), c.POINTER(Frame), c.POINTER(Result)]
        lib.machine_program_run.restype = c.c_int

    def _program(self, raw):
        if self.gate.library is None:
            raise MachineError("NATIVE_GATE_NOT_CONFIGURED")
        require_keys(raw, {"version", "fields", "instructions"}, "native program")
        if raw["version"] != 1 or type(raw["version"]) is not int:
            raise MachineError("NATIVE_PROGRAM_VERSION")
        fields, instructions = raw["fields"], raw["instructions"]
        if (not isinstance(fields, list) or len(fields) > 32 or
                not isinstance(instructions, list) or not 1 <= len(instructions) <= 128):
            raise MachineError("BOUNDED_NATIVE_PROGRAM_REQUIRED")
        p = Program(version=1, count=len(instructions))
        for index, unit in enumerate(fields):
            if not isinstance(unit, str) or unit not in UNITS or unit == "none":
                raise MachineError("NATIVE_FIELD_UNIT_REQUIRED")
            p.field_units[index] = UNITS[unit]
        for index, instruction in enumerate(instructions):
            require_keys(instruction, {"op", "dst", "a", "b", "c", "unit", "value"}, "instruction")
            if (not isinstance(instruction["op"], str) or instruction["op"] not in OPS or
                    not isinstance(instruction["unit"], str) or instruction["unit"] not in UNITS):
                raise MachineError("NATIVE_OPCODE_OR_UNIT_REQUIRED")
            values = [integer(instruction[key], 0, 31) for key in ["dst", "a", "b", "c"]]
            p.instructions[index] = Instruction(OPS[instruction["op"]], *values, UNITS[instruction["unit"]],
                                                  integer(instruction["value"], -(2**63), 2**63 - 1))
        return p

    def compile(self, raw):
        program, code, pc = self._program(raw), c.c_uint32(), c.c_uint32()
        if self.gate.library.machine_program_validate(c.byref(program), c.byref(code), c.byref(pc)) != 0:
            raise MachineError("NATIVE_PROGRAM_BOUNDARY_FAILED")
        if code.value >= len(CODES):
            raise MachineError("NATIVE_PROGRAM_UNKNOWN_RESULT")
        return {"accepted": code.value == 0, "code": CODES[code.value], "instruction": pc.value,
                "program_sha256": digest(raw), "library_sha256": self.gate.sha256,
                "execution_authority": "NONE", "max_instructions": 128, "loops": "FORBIDDEN"}

    def evaluate(self, raw):
        require_keys(raw, {"program", "frame"}, "program evaluation")
        program = self._program(raw["program"])
        data = raw["frame"]
        require_keys(data, {name for name, _ in FRAME_FIELDS} | {"values"}, "numeric state")
        values = data["values"]
        if not isinstance(values, list) or len(values) != len(raw["program"]["fields"]):
            raise MachineError("NATIVE_STATE_FIELDS_MISMATCH")
        frame = Frame()
        for name, kind in FRAME_FIELDS:
            setattr(frame, name, integer(data[name], 0, 2**(c.sizeof(kind) * 8) - 1))
        for index, value in enumerate(values):
            frame.values[index] = integer(value, -(2**63), 2**63 - 1)
        output = Result()
        if (self.gate.library.machine_program_run(c.byref(program), c.byref(frame), c.byref(output)) != 0
                or output.execution_authority != 0 or output.code >= len(CODES)):
            raise MachineError("NATIVE_PROGRAM_AUTHORITY_BOUNDARY_FAILED")
        return {"accepted": output.code == 0, "code": CODES[output.code],
                "candidate": ACTIONS.get(output.candidate), "score_microunits": output.score,
                "instruction": output.failed_instruction, "sequence": output.sequence,
                "valid_until_ns": output.valid_until_ns, "program_sha256": digest(raw["program"]),
                "state_sha256": digest(data), "library_sha256": self.gate.sha256,
                "execution_authority": "NONE", "language_model_calls": 0,
                "scope": "CANDIDATE_ONLY_NOT_ORDER_APPROVAL"}
