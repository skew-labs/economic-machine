"""Static analyzer for bounded, typed, versioned Economic IR.

The registry names future opcodes, but a program containing an unsupported
operation is rejected at compile time. No LLM text is accepted by the kernel.
"""

from .spec import ISA_V1, ISA_V2, ISA_V3, SPEC
from .transitions import transition
from .values import MachineError, canonical, decimal, digest, ident, require_keys, utc

ISA_VERSION = ISA_V1
GUARDED_ISA_VERSION = ISA_V2
GRAPH_ISA_VERSION = ISA_V3
ISA_VERSIONS = set(SPEC["versions"])
IMPLEMENTED = {name for name, rule in SPEC["opcodes"].items()
               if rule["status"] == "implemented"}
RESERVED = set(SPEC["opcodes"]) - IMPLEMENTED
ORDER = {name: rule["order"] for name, rule in SPEC["opcodes"].items()
         if rule["status"] == "implemented"}


def _check_sandbox(value: dict) -> dict:
    require_keys(value, {"asset", "capital_limit", "max_exposure", "max_total_cost",
                         "max_daily_loss", "allowed_protocols", "expires_at"}, "sandbox")
    ident(value["asset"], "sandbox asset")
    for key in ("capital_limit", "max_exposure", "max_total_cost", "max_daily_loss"):
        decimal(value[key])
    if decimal(value["max_exposure"]) > decimal(value["capital_limit"]):
        raise MachineError("exposure cannot exceed capital limit")
    if (not isinstance(value["allowed_protocols"], list)
            or not value["allowed_protocols"]
            or len(value["allowed_protocols"]) > 32):
        raise MachineError("allowed_protocols must be a nonempty bounded list")
    for protocol in value["allowed_protocols"]:
        ident(protocol, "protocol")
    if len(set(value["allowed_protocols"])) != len(value["allowed_protocols"]):
        raise MachineError("duplicate allowed protocol")
    utc(value["expires_at"])
    return value


def _check_instruction(ins: dict, registers: set[str], seen: list[str],
                       seen_instructions: list[dict], sandbox: dict,
                       isa_version: str, position: int | None = None,
                       instruction_count: int | None = None) -> None:
    if not isinstance(ins, dict) or "op" not in ins:
        raise MachineError("instruction must have an opcode")
    op = ins["op"]
    if not isinstance(op, str):
        raise MachineError("opcode must be a string")
    if op in RESERVED:
        raise MachineError("reserved opcode is not implemented: " + op)
    if op not in IMPLEMENTED:
        raise MachineError("unknown opcode: " + str(op))
    rule = SPEC["opcodes"][op]
    if isa_version not in rule["versions"]:
        raise MachineError(op + " requires " + rule["versions"][0])
    if seen and ORDER[op] < ORDER[seen[-1]]:
        raise MachineError("opcode order violates the transition grammar")
    require_keys(ins, set(rule["fields"]), op)
    if op == "OBSERVE":
        ident(ins["path"], "state path")
        ident(ins["as"], "register")
        if ins["as"] in registers:
            raise MachineError("register is single-assignment")
        registers.add(ins["as"])
        if type(ins["max_age_ms"]) is not int or not 1 <= ins["max_age_ms"] <= 86400000:
            raise MachineError("invalid observation age")
    elif op == "PRICE":
        if (not isinstance(ins["paths"], list) or not 2 <= len(ins["paths"]) <= 5
                or any(not isinstance(path, str) for path in ins["paths"])
                or len(set(ins["paths"])) != len(ins["paths"])):
            raise MachineError("PRICE requires two to five distinct sources")
        for path in ins["paths"]:
            ident(path, "price path")
        ident(ins["as"], "register")
        if ins["as"] in registers:
            raise MachineError("register is single-assignment")
        registers.add(ins["as"])
        if (type(ins["max_age_ms"]) is not int or ins["max_age_ms"] < 1
                or ins["max_age_ms"] > 86400000):
            raise MachineError("invalid price age")
        if (type(ins["max_dispersion_bps"]) is not int
                or not 0 <= ins["max_dispersion_bps"] <= 10000):
            raise MachineError("invalid price dispersion")
    elif op in {"ASSERT", "GUARD", "BRANCH"}:
        ident(ins["register"], "assert register")
        if ins["register"] not in registers:
            raise MachineError(op + " uses an undefined register")
        if (not isinstance(ins["comparison"], str)
                or ins["comparison"] not in {"LT", "LTE", "EQ", "GTE", "GT"}):
            raise MachineError("unsupported comparison")
        decimal(ins["value"], signed=True)
        ident(ins["unit"], "unit")
        if op == "GUARD":
            if (not isinstance(ins["on_false"], str)
                    or ins["on_false"] not in {"HOLD", "ESCALATE", "ABORT"}):
                raise MachineError("GUARD requires a bounded false branch")
            ident(ins["reason"], "guard reason")
        if op == "BRANCH" and (position is None or instruction_count is None
                    or any(type(ins[key]) is not int or not position < ins[key] < instruction_count
                           for key in ("if_true", "if_false"))
                    or ins["if_true"] == ins["if_false"]):
            raise MachineError("BRANCH targets must be distinct forward instruction indices")
    elif op == "QUOTE":
        if ins["asset"] != sandbox["asset"]:
            raise MachineError("quote asset outside sandbox")
        if decimal(ins["amount"]) <= 0:
            raise MachineError("quote amount must be positive")
        if type(ins["max_age_ms"]) is not int or not 1 <= ins["max_age_ms"] <= 86400000:
            raise MachineError("invalid quote age")
    elif op == "SCORE":
        if ins["objective"] != "MIN_TOTAL_COST" or "QUOTE" not in seen:
            raise MachineError("SCORE needs a QUOTE and MIN_TOTAL_COST")
        ident(ins["as"], "register")
        if ins["as"] in registers:
            raise MachineError("register is single-assignment")
        registers.add(ins["as"])
    elif op == "ALLOCATE":
        ident(ins["route_register"], "route register")
        if (ins["asset"] != sandbox["asset"] or "SCORE" not in seen
                or ins["route_register"] not in registers):
            raise MachineError("allocation needs an in-sandbox scored route")
        if decimal(ins["amount"]) <= 0:
            raise MachineError("allocation amount must be positive")
        quoted = next(item for item in seen_instructions if item["op"] == "QUOTE")
        if decimal(ins["amount"]) != decimal(quoted["amount"]):
            raise MachineError("allocation amount must equal quoted amount")
        scored = next(item for item in seen_instructions if item["op"] == "SCORE")
        if ins["route_register"] != scored["as"]:
            raise MachineError("allocation route must come from SCORE")
    elif op in {"SIMULATE", "VERIFY", "PREPARE", "SETTLE"}:
        pass
    elif op in {"ESCALATE", "ABORT", "HOLD"}:
        ident(ins["reason"], "reason")
    seen.append(op)
    seen_instructions.append(ins)


def _check_complete_path(seen: list[str]) -> None:
    if seen[-1] in SPEC["early_terminal_opcodes"]:
        if any(op in seen for op in ("PREPARE", "SETTLE")):
            raise MachineError("early terminal cannot prepare execution")
    else:
        required = set(SPEC["normal_path_required"])
        if not required.issubset(seen) or seen[-1] != "SETTLE":
            raise MachineError("normal program must simulate, verify, prepare and await settlement")
        for op in required:
            if seen.count(op) != 1:
                raise MachineError("single transaction path must use each stage once")


def _check_graph(instructions: list[dict], sandbox: dict) -> None:
    """Explore every bounded forward path, including both arms of each branch."""
    if sum(isinstance(ins, dict) and ins.get("op") == "BRANCH" for ins in instructions) > SPEC["limits"]["branches"]:
        raise MachineError("at most eight BRANCH instructions are supported")
    reachable: set[int] = set()
    completed = 0

    def walk(pc: int, seen: list[str], history: list[dict], registers: set[str],
             phase: str) -> None:
        nonlocal completed
        if pc >= len(instructions):
            raise MachineError("control-flow path falls off the program")
        if completed >= SPEC["limits"]["paths"]:
            raise MachineError("control-flow path limit exceeded")
        ins = instructions[pc]
        seen, history, registers = seen.copy(), history.copy(), registers.copy()
        _check_instruction(ins, registers, seen, history, sandbox, GRAPH_ISA_VERSION,
                           pc, len(instructions))
        next_phase = transition(phase, ins["op"])
        reachable.add(pc)
        if ins["op"] == "BRANCH":
            walk(ins["if_true"], seen, history, registers, next_phase)
            walk(ins["if_false"], seen, history, registers, next_phase)
        elif ins["op"] in SPEC["terminal_opcodes"]:
            _check_complete_path(seen)
            completed += 1
        else:
            walk(pc + 1, seen, history, registers, next_phase)

    walk(0, [], [], set(), "CREATED")
    if len(reachable) != len(instructions):
        raise MachineError("unreachable instruction in control-flow graph")


def compile_program(source: dict) -> dict:
    """Compile a reviewed typed program; reject partial or overpowered programs."""
    require_keys(source, {"schema_version", "program_id", "owner_id", "agent_id",
                          "network", "sandbox", "instructions"}, "EconomicProgram")
    if (not isinstance(source["schema_version"], str)
            or source["schema_version"] not in ISA_VERSIONS):
        raise MachineError("unsupported ISA version")
    for key in ("program_id", "owner_id", "agent_id", "network"):
        ident(source[key], key)
    sandbox = _check_sandbox(source["sandbox"])
    instructions = source["instructions"]
    if not isinstance(instructions, list) or not 1 <= len(instructions) <= SPEC["limits"]["instructions"]:
        raise MachineError("instructions must contain 1 to 64 operations")
    if source["schema_version"] == GRAPH_ISA_VERSION:
        _check_graph(instructions, sandbox)
    else:
        seen: list[str] = []
        seen_instructions: list[dict] = []
        registers: set[str] = set()
        for ins in instructions:
            _check_instruction(ins, registers, seen, seen_instructions, sandbox,
                               source["schema_version"])
        _check_complete_path(seen)
    # Roundtrip blocks custom Python objects and floating point even in unused fields.
    body = json_roundtrip(source)
    return {**body, "program_hash": digest(body)}


def json_roundtrip(value: dict) -> dict:
    import json
    return json.loads(canonical(value))
