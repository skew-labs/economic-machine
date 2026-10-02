"""Pure Economic CPU: typed instructions, capital invariants, transition receipt.

No external adapter, LLM, wallet, database, or network call is in this module.
The result is a simulation/authorization intent; it never broadcasts funds.
"""

import re
from datetime import datetime
from decimal import Decimal, Inexact, Rounded, localcontext
from statistics import median

from .compiler import GRAPH_ISA_VERSION, GUARDED_ISA_VERSION, ISA_VERSION, ISA_VERSIONS, compile_program
from .state import normalize_state, state_root
from .transitions import halt, transition
from .values import MachineError, decimal, decstr, digest, ident, require_keys, utc

KERNEL_VERSION = "economic-kernel-0.2.0"
GUARDED_KERNEL_VERSION = "economic-kernel-0.3.0"
GRAPH_KERNEL_VERSION = "economic-kernel-0.4.0"


class Halt(Exception):
    def __init__(self, state: str, reason: str):
        self.state = state
        self.reason = reason
        super().__init__(reason)


def _age_us(at: str, observed: str) -> int:
    delta = datetime.fromisoformat(at) - datetime.fromisoformat(observed)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000
            + delta.microseconds)


def _fact(state: dict, path: str, at: str, max_age_ms: int) -> tuple[Decimal, str]:
    fact = state["facts"].get(path)
    if fact is None or fact["quality"] not in {"VALID", "VALID_ZERO"}:
        raise Halt("ESCALATED", "FACT_UNAVAILABLE:" + path)
    age = _age_us(at, fact["observed_at"])
    if age < 0 or age > max_age_ms * 1000:
        raise Halt("ESCALATED", "FACT_STALE:" + path)
    return decimal(fact["value"], signed=True), fact["unit"]


def _compare(lhs: Decimal, relation: str, rhs: Decimal) -> bool:
    return {"LT": lhs < rhs, "LTE": lhs <= rhs, "EQ": lhs == rhs,
            "GTE": lhs >= rhs, "GT": lhs > rhs}[relation]


def _price(ins: dict, state: dict, at: str) -> tuple[Decimal, str]:
    values = []
    units = set()
    sources = set()
    for path in ins["paths"]:
        amount, unit = _fact(state, path, at, ins["max_age_ms"])
        if amount <= 0:
            raise Halt("ESCALATED", "INVALID_PRICE:" + path)
        source_id = state["facts"][path]["source_id"]
        if source_id in sources:
            raise Halt("ESCALATED", "PRICE_SOURCES_NOT_INDEPENDENT")
        sources.add(source_id)
        values.append(amount)
        units.add(unit)
    if len(units) != 1:
        raise Halt("ESCALATED", "PRICE_UNITS_DISAGREE")
    middle = median(values)
    if (max(values) - min(values)) * Decimal(10000) > Decimal(ins["max_dispersion_bps"]) * middle:
        raise Halt("ESCALATED", "PRICE_DISPERSION")
    return middle, next(iter(units))


def _quotes(ins: dict, state: dict, at: str, allowed: set[str]) -> list[dict]:
    eligible = []
    for quote in state["quotes"].values():
        if (quote["asset"] != ins["asset"] or quote["protocol"] not in allowed
                or decimal(quote["amount"]) != decimal(ins["amount"])):
            continue
        age = _age_us(at, quote["observed_at"])
        if age < 0 or age > ins["max_age_ms"] * 1000 or at >= quote["expires_at"]:
            continue
        eligible.append(quote)
    if not eligible:
        raise Halt("ESCALATED", "NO_VALID_QUOTE")
    return eligible


def _total_cost(quote: dict) -> Decimal:
    return sum((decimal(quote[key]) for key in ("fee", "slippage_cost", "gas_cost")),
               Decimal(0))


def _invariants(program: dict, state: dict, action: dict, expected: dict,
                at: str, pending: list[dict]) -> list[dict]:
    policy = program["sandbox"]
    asset = policy["asset"]
    amount = decimal(action["amount"])
    cost = decimal(action["total_cost"])
    balance = state["balances"].get(asset)
    exposure = state["exposures"].get(program["agent_id"], {}).get(asset)
    loss = state["daily_losses"].get(program["agent_id"], {}).get(asset)
    reserved_capital = sum((decimal(item["amount"]) + decimal(item["cost"])
                            for item in pending if item["owner_id"] == program["owner_id"]
                            and item["asset"] == asset
                            and item["network"] == program["network"]), Decimal(0))
    reserved_exposure = sum((decimal(item["amount"]) for item in pending
                             if item["owner_id"] == program["owner_id"]
                             and item["asset"] == asset and item["network"] == program["network"]
                             and item["agent_id"] == program["agent_id"]), Decimal(0))
    reserved_loss = sum((decimal(item["cost"]) for item in pending
                         if item["owner_id"] == program["owner_id"]
                         and item["asset"] == asset and item["network"] == program["network"]
                         and item["agent_id"] == program["agent_id"]), Decimal(0))
    checks = [
        ("OWNER", state["owner_id"] == program["owner_id"]),
        ("NETWORK", state["network"] == program["network"]),
        ("POLICY_TTL", at < policy["expires_at"]),
        ("ALLOWED_PROTOCOL", action["protocol"] in policy["allowed_protocols"]),
        ("CAPITAL_LIMIT", amount <= decimal(policy["capital_limit"])),
        ("COST_LIMIT", cost <= decimal(policy["max_total_cost"])),
        ("QUOTE_TTL", at < action["quote_expires_at"]),
        ("KNOWN_BALANCE", balance is not None),
        ("KNOWN_EXPOSURE", exposure is not None),
        ("KNOWN_DAILY_LOSS", loss is not None),
    ]
    if balance is not None:
        checks.append(("NONNEGATIVE_BALANCE", decimal(balance) >= reserved_capital + amount + cost))
    if exposure is not None:
        checks.append(("EXPOSURE_CAP", decimal(exposure) + reserved_exposure + amount
                       <= decimal(policy["max_exposure"])))
    if loss is not None:
        checks.append(("DAILY_LOSS_CAP", decimal(loss) + reserved_loss + cost
                       <= decimal(policy["max_daily_loss"])))
    if balance is not None and exposure is not None and loss is not None:
        checks.extend([
            ("EXPECTED_BALANCE", decimal(expected["balance_after"], signed=True) == decimal(balance) - amount - cost),
            ("EXPECTED_EXPOSURE", decimal(expected["exposure_after"]) == decimal(exposure) + amount),
            ("EXPECTED_DAILY_LOSS", decimal(expected["daily_loss_after"]) == decimal(loss) + cost),
        ])
    return [{"invariant": code, "pass": passed} for code, passed in checks]


class EconomicKernel:
    def evaluate(self, compiled: dict, world: dict, *, at: str,
                 pending_reservations: list[dict] | None = None) -> dict:
        with localcontext() as context:
            context.prec = 256
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            return self._evaluate(compiled, world, at=at,
                                  pending_reservations=pending_reservations)

    def _evaluate(self, compiled: dict, world: dict, *, at: str,
                  pending_reservations: list[dict] | None = None) -> dict:
        if not isinstance(compiled, dict) or "program_hash" not in compiled:
            raise MachineError("compiled program required")
        body = {key: value for key, value in compiled.items() if key != "program_hash"}
        checked = compile_program(body)
        if checked["program_hash"] != compiled["program_hash"]:
            raise MachineError("program hash mismatch")
        state = normalize_state(world)
        at = utc(at)
        if datetime.fromisoformat(at) < datetime.fromisoformat(state["as_of"]):
            raise MachineError("decision time precedes state")
        if compiled["schema_version"] not in ISA_VERSIONS:
            raise MachineError("wrong ISA")
        pending = []
        for item in pending_reservations or []:
            keys = {"reservation_id", "program_id", "owner_id", "agent_id", "network",
                    "asset", "amount", "cost", "expires_at"}
            if isinstance(item, dict) and "status" in item:
                keys.add("status")
            require_keys(item, keys, "reservation")
            if (not isinstance(item["reservation_id"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", item["reservation_id"]) is None):
                raise MachineError("invalid reservation id")
            for key in ("program_id", "owner_id", "agent_id", "network", "asset"):
                ident(item[key], key)
            decimal(item["amount"])
            decimal(item["cost"])
            utc(item["expires_at"])
            reservation_status = item.get("status", "ACTIVE")
            if reservation_status not in {"ACTIVE", "EXECUTION_LOCKED"}:
                raise MachineError("invalid reservation status")
            if item["owner_id"] != state["owner_id"]:
                raise MachineError("foreign-owner reservation")
            if reservation_status == "EXECUTION_LOCKED" or item["expires_at"] > at:
                pending.append(item)
        pending.sort(key=lambda item: item["reservation_id"])
        if len({item["reservation_id"] for item in pending}) != len(pending):
            raise MachineError("duplicate reservation")
        registers: dict[str, tuple[Decimal, str] | dict] = {}
        trace = []
        valid_quotes: list[dict] = []
        action: dict | None = None
        expected: dict | None = None
        intent: dict | None = None
        invariant_results: list[dict] = []
        phase = "CREATED"
        phase_trace = []
        terminal = "ABORTED"
        reason = "NO_TERMINAL_INSTRUCTION"
        try:
            if state["owner_id"] != compiled["owner_id"]:
                raise Halt("ABORTED", "OWNER_MISMATCH")
            if state["network"] != compiled["network"]:
                raise Halt("ABORTED", "NETWORK_MISMATCH")
            if at >= compiled["sandbox"]["expires_at"]:
                raise Halt("ABORTED", "POLICY_EXPIRED")
            pc = 0
            while pc < len(compiled["instructions"]):
                index = pc
                ins = compiled["instructions"][index]
                op = ins["op"]
                next_phase = transition(phase, op)
                guard_failure = None
                branch_target = None
                if op == "OBSERVE":
                    value, unit = _fact(state, ins["path"], at, ins["max_age_ms"])
                    registers[ins["as"]] = (value, unit)
                    result = {"register": ins["as"], "value": decstr(value), "unit": unit}
                elif op == "PRICE":
                    value, unit = _price(ins, state, at)
                    registers[ins["as"]] = (value, unit)
                    result = {"register": ins["as"], "value": decstr(value), "unit": unit}
                elif op == "ASSERT":
                    value, unit = registers[ins["register"]]
                    if unit != ins["unit"]:
                        raise Halt("ESCALATED", "ASSERT_UNIT_MISMATCH")
                    if not _compare(value, ins["comparison"], decimal(ins["value"], signed=True)):
                        raise Halt("ABORTED", "ASSERT_FALSE")
                    result = {"assertion": "PASS"}
                elif op == "GUARD":
                    value, unit = registers[ins["register"]]
                    if unit != ins["unit"]:
                        raise Halt("ESCALATED", "GUARD_UNIT_MISMATCH")
                    passed = _compare(value, ins["comparison"],
                                      decimal(ins["value"], signed=True))
                    result = {"register": ins["register"], "observed": decstr(value),
                              "unit": unit, "comparison": ins["comparison"],
                              "threshold": decstr(decimal(ins["value"], signed=True)),
                              "pass": passed}
                    if not passed:
                        guard_failure = ({"HOLD": "HELD", "ESCALATE": "ESCALATED",
                                          "ABORT": "ABORTED"}[ins["on_false"]],
                                         "GUARD_FALSE:" + ins["reason"])
                elif op == "BRANCH":
                    value, unit = registers[ins["register"]]
                    if unit != ins["unit"]:
                        raise Halt("ESCALATED", "BRANCH_UNIT_MISMATCH")
                    passed = _compare(value, ins["comparison"],
                                      decimal(ins["value"], signed=True))
                    branch_target = ins["if_true"] if passed else ins["if_false"]
                    result = {"register": ins["register"], "observed": decstr(value),
                              "unit": unit, "comparison": ins["comparison"],
                              "threshold": decstr(decimal(ins["value"], signed=True)),
                              "pass": passed, "next_instruction": branch_target}
                elif op == "QUOTE":
                    valid_quotes = _quotes(ins, state, at,
                                           set(compiled["sandbox"]["allowed_protocols"]))
                    result = {"eligible_quote_ids": sorted(q["quote_id"] for q in valid_quotes)}
                elif op == "SCORE":
                    selected = min(valid_quotes, key=lambda q: (_total_cost(q), q["protocol"], q["quote_id"]))
                    registers[ins["as"]] = selected
                    result = {"selected_quote_id": selected["quote_id"],
                              "total_cost": decstr(_total_cost(selected))}
                elif op == "ALLOCATE":
                    quote = registers[ins["route_register"]]
                    action = {"kind": "ALLOCATE", "asset": ins["asset"],
                              "amount": decstr(decimal(ins["amount"])),
                              "protocol": quote["protocol"], "quote_id": quote["quote_id"],
                              "quote_source_hash": quote["source_hash"],
                              "quote_expires_at": quote["expires_at"],
                              "total_cost": decstr(_total_cost(quote))}
                    result = {"action_hash": digest(action)}
                elif op == "SIMULATE":
                    asset = action["asset"]
                    agent = compiled["agent_id"]
                    balance = state["balances"].get(asset)
                    exposure = state["exposures"].get(agent, {}).get(asset)
                    loss = state["daily_losses"].get(agent, {}).get(asset)
                    if balance is None or exposure is None or loss is None:
                        raise Halt("ESCALATED", "CAPITAL_STATE_UNKNOWN")
                    amount, cost = decimal(action["amount"]), decimal(action["total_cost"])
                    expected = {"balance_after": decstr(decimal(balance) - amount - cost),
                                "exposure_after": decstr(decimal(exposure) + amount),
                                "daily_loss_after": decstr(decimal(loss) + cost),
                                "simulation_kind": "LOCAL_ACCOUNTING_ONLY"}
                    result = {"expected_hash": digest(expected)}
                elif op == "VERIFY":
                    invariant_results = _invariants(compiled, state, action, expected, at, pending)
                    failed = [item["invariant"] for item in invariant_results if not item["pass"]]
                    if failed:
                        raise Halt("ABORTED", "INVARIANT:" + ",".join(failed))
                    result = {"invariants_passed": len(invariant_results)}
                elif op == "PREPARE":
                    intent = {"program_hash": compiled["program_hash"],
                              "owner_id": compiled["owner_id"],
                              "agent_id": compiled["agent_id"],
                              "network": compiled["network"],
                              "state_root": state_root(state), "action": action,
                              "expected": expected,
                              "expires_at": min(compiled["sandbox"]["expires_at"],
                                                action["quote_expires_at"]),
                              "authority": "USER_AUTHORIZATION_REQUIRED",
                              "execution_status": "SIMULATION_ONLY_NO_CHAIN_ADAPTER"}
                    intent["intent_hash"] = digest(intent)
                    result = {"intent_hash": intent["intent_hash"]}
                elif op == "SETTLE":
                    terminal, reason = "AWAITING_AUTHORIZATION", "NO_SIGNED_TRANSACTION"
                    result = {"settlement": "NOT_SUBMITTED"}
                elif op in {"ESCALATE", "ABORT"}:
                    terminal = "ESCALATED" if op == "ESCALATE" else "ABORTED"
                    reason = ins["reason"]
                    result = {"terminal": terminal}
                elif op == "HOLD":
                    terminal, reason = "HELD", ins["reason"]
                    intent = None
                    result = {"terminal": terminal}
                else:
                    raise MachineError("compiler allowed an unknown opcode")
                trace.append({"step": index, "op": op, "result_hash": digest(result),
                              "result": result})
                phase_trace.append({"from": phase, "opcode": op, "to": next_phase})
                phase = next_phase
                if guard_failure is not None:
                    raise Halt(*guard_failure)
                if op in {"SETTLE", "ESCALATE", "ABORT", "HOLD"}:
                    break
                pc = branch_target if branch_target is not None else pc + 1
        except Halt as halted:
            terminal, reason = halted.state, halted.reason
            phase_trace.append({"from": phase, "opcode": "HALT", "to": halt(phase, terminal)})
            phase = terminal
            trace.append({"step": len(trace), "op": "HALT",
                          "result_hash": digest({"state": terminal, "reason": reason}),
                          "result": {"state": terminal, "reason": reason}})
            intent = None
        receipt_version, kernel_version = {
            ISA_VERSION: ("economic-receipt-2", KERNEL_VERSION),
            GUARDED_ISA_VERSION: ("economic-receipt-3", GUARDED_KERNEL_VERSION),
            GRAPH_ISA_VERSION: ("economic-receipt-4", GRAPH_KERNEL_VERSION),
        }[compiled["schema_version"]]
        receipt = {"schema_version": receipt_version, "kernel_version": kernel_version,
                   "program_id": compiled["program_id"], "program_hash": compiled["program_hash"],
                   "owner_id": compiled["owner_id"],
                   "agent_id": compiled["agent_id"], "network": compiled["network"],
                   "state_root": state_root(state), "state_sequence": state["sequence"],
                   "pending_reservations": pending, "reservation_root": digest(pending),
                   "at": at, "terminal_state": terminal, "reason_code": reason,
                   "action": action, "intent": intent, "invariants": invariant_results,
                   "trace": trace, "phase_trace": phase_trace, "phase": phase,
                   "chain_status": "NOT_SUBMITTED",
                   "signature_status": "NOT_SIGNED"}
        receipt["receipt_hash"] = digest(receipt)
        return receipt
