"""Typed bridge to the native economic primitive library, in the same workspace.

Financial values are integer micro-units. Inputs describe assumptions, not
authenticated prices or signed positions. A computed candidate never becomes
an order, permission, wallet signature or external payment automatically.
"""

import ctypes as c
import hashlib
import os
import re
import time
from pathlib import Path

from economic_machine.values import MachineError, canonical, digest, require_keys

I64, U64, U32, BOOL = c.c_int64, c.c_uint64, c.c_uint32, c.c_bool


def structure(name, fields):
    return type(name, (c.Structure,), {"_fields_": fields})


Envelope = structure("Envelope", [(name, U64) for name in (
    "sequence", "expected_sequence", "observed_ns", "now_ns", "max_age_ns",
    "deadline_ns", "policy_version", "expected_policy_version")])
OracleObservation = structure("OracleObservation", [
    ("source", U64), ("instrument", U64), ("sequence", U64), ("observed_ns", U64),
    ("price", I64), ("confidence_radius", I64), ("adapter_verified", BOOL)])
OraclePolicy = structure("OraclePolicy", [
    ("instrument", U64), ("now_ns", U64), ("max_age_ns", U64), ("max_skew_ns", U64),
    ("min_sources", U32), ("max_dispersion_bps", I64), ("max_confidence_bps", I64),
    ("allowed_sources", U64 * 16), ("source_count", U32)])
OraclePrice = structure("OraclePrice", [
    *[(name, I64) for name in ("median", "low", "high", "dispersion_bps")],
    ("sources", U32), *[(name, U64) for name in (
        "oldest_ns", "newest_ns", "valid_until_ns", "diagnostic_fingerprint")]])
OracleRequest = structure("OracleRequest", [
    ("policy", OraclePolicy), ("observations", OracleObservation * 16), ("count", U32)])
LendingMarket = structure("LendingMarket", [(name, I64) for name in (
    "cash", "borrowed", "reserves", "base_apr", "slope_apr", "jump_apr", "kink", "reserve_factor")])
LendingRates = structure("LendingRates", [(name, I64) for name in (
    "utilization", "borrow_apr", "supply_apr", "available_cash")])
YieldTerms = structure("YieldTerms", [(name, I64) for name in (
    "principal", "base_apr", "incentive_apr", "incentive_retention", "exit_haircut",
    "entry_fee", "exit_fee", "horizon_seconds", "lock_seconds", "exit_available")])
YieldEstimate = structure("YieldEstimate", [(name, I64) for name in (
    "base_income", "incentive_income", "exit_loss", "fees", "net_income", "terminal_value")])
CollateralAsset = structure("CollateralAsset", [
    ("asset", U64), *[(name, I64) for name in (
        "quantity", "conservative_price", "collateral_factor", "liquidation_factor")]])
DebtAsset = structure("DebtAsset", [
    ("asset", U64), *[(name, I64) for name in ("principal", "accrued_interest", "conservative_price")]])
Health = structure("Health", [
    *[(name, I64) for name in ("collateral_value", "borrow_capacity", "liquidation_capacity",
        "debt_value", "headroom", "health_factor")], ("debt_free", BOOL), ("liquidatable", BOOL)])
HealthRequest = structure("HealthRequest", [
    ("collateral", CollateralAsset * 32), ("debts", DebtAsset * 32),
    ("collateral_count", U32), ("debt_count", U32)])
LinearPosition = structure("LinearPosition", [
    ("instrument", U64), *[(name, I64) for name in ("signed_quantity", "entry_price", "mark_price",
        "collateral", "accrued_funding", "unpaid_fees", "initial_margin_rate", "maintenance_margin_rate")]])
PositionRisk = structure("PositionRisk", [
    *[(name, I64) for name in ("absolute_quantity", "notional", "unrealized_pnl", "equity",
        "initial_margin", "maintenance_margin", "margin_headroom", "leverage")], ("liquidatable", BOOL)])
FundingRequest = structure("FundingRequest", [(name, I64) for name in (
    "signed_quantity", "settlement_price", "funding_rate")])
FundingCashFlow = structure("FundingCashFlow", [("notional", I64), ("signed_payment", I64)])
ExecutionVenue = structure("ExecutionVenue", [
    ("id", U64), *[(name, I64) for name in ("executable_price", "available_quantity", "quantity_step",
        "fee_rate", "fixed_cost", "min_notional")], ("allowed", BOOL), ("envelope", Envelope)])
RecoveryPolicy = structure("RecoveryPolicy", [
    *[(name, I64) for name in ("minimum_equity", "target_margin_headroom", "maximum_leverage",
        "maximum_close_quantity", "maximum_total_cost", "max_slippage_bps", "maximum_close_fraction")],
    ("maximum_lots_examined", U32), ("deadline_ns", U64)])
RecoveryChoice = structure("RecoveryChoice", [
    ("action", U32), ("reason", U32), ("error", U32), ("venue_id", U64),
    *[(name, I64) for name in ("close_quantity", "execution_price", "total_cost", "equity_after",
        "headroom_after", "leverage_after")], ("valid_until_ns", U64),
    ("examined", U32), ("feasible", U32), ("execution_authority", U64)])
DerivativeRecoveryRequest = structure("DerivativeRecoveryRequest", [
    ("position", LinearPosition), ("policy", RecoveryPolicy), ("venues", ExecutionVenue * 16),
    ("count", U32), ("state", Envelope)])
RepayPolicy = structure("RepayPolicy", [(name, I64) for name in (
    "target_health_factor", "max_payment_value", "available_cash_value", "cash_reserve_value",
    "maximum_cost", "execution_cost")])
RepayChoice = structure("RepayChoice", [
    ("action", U32), ("reason", U32), *[(name, I64) for name in ("payment_value", "debt_value_after",
        "cash_value_after", "health_factor_after")], ("debt_free_after", BOOL),
    ("valid_until_ns", U64), ("execution_authority", U64)])
RepaymentRequest = structure("RepaymentRequest", [("health", Health), ("policy", RepayPolicy), ("state", Envelope)])
RebalanceTerms = structure("RebalanceTerms", [
    *[(name, I64) for name in ("current_expected_income", "proposed_expected_income", "trading_cost",
        "exit_cost", "settlement_cost", "model_uncertainty_buffer", "minimum_improvement")],
    ("last_rebalance_ns", U64), ("cooldown_ns", U64)])
RebalanceChoice = structure("RebalanceChoice", [
    ("action", U32), ("reason", U32), *[(name, I64) for name in ("gross_improvement", "total_cost",
        "conservative_improvement")], ("valid_until_ns", U64), ("execution_authority", U64)])
RebalanceRequest = structure("RebalanceRequest", [("terms", RebalanceTerms), ("state", Envelope)])
ConstantProductPool = structure("ConstantProductPool", [
    ("pool_id", U64), ("asset_in", U64), ("asset_out", U64),
    *[(name, I64) for name in ("reserve_in", "reserve_out", "fee_rate", "fixed_cost")], ("envelope", Envelope)])
SwapQuote = structure("SwapQuote", [
    ("pool_id", U64), ("asset_in", U64), ("asset_out", U64),
    *[(name, I64) for name in ("amount_in", "amount_out", "fee_in", "price_impact_bps", "effective_price",
        "fixed_cost", "reserve_in_after", "reserve_out_after")], ("valid_until_ns", U64)])
SwapRequest = structure("SwapRequest", [("pool", ConstantProductPool), ("amount", I64), ("exact_output", BOOL)])
SwapRoute = structure("SwapRoute", [
    ("pools", ConstantProductPool * 4), ("count", U32),
    *[(name, I64) for name in ("amount_in", "minimum_output", "max_cost", "max_impact_bps")]])
RouteQuote = structure("RouteQuote", [
    ("hops", SwapQuote * 4), ("count", U32), *[(name, I64) for name in (
        "amount_in", "amount_out", "fixed_cost")], ("valid_until_ns", U64)])
Product = structure("Product", [
    ("id", U64), ("group", U32), *[(name, I64) for name in ("current_value", "expected_net_return_bps",
        "min_weight_bps", "max_weight_bps", "available_capacity", "exit_capacity", "exit_delay_seconds",
        "turnover_cost_bps", "fixed_entry_cost", "fixed_exit_cost")],
    ("scenario_loss_bps", I64 * 16), ("liquid", BOOL), ("enabled", BOOL)])
AllocationPolicy = structure("AllocationPolicy", [
    *[(name, I64) for name in ("capital", "cash_floor", "max_turnover", "max_cost", "max_stress_loss",
        "horizon_seconds")], ("group_caps_bps", I64 * 16), ("group_count", U32), ("scenario_count", U32)])
AllocationScore = structure("AllocationScore", [
    ("candidate_id", U64), ("amounts", I64 * 16), ("scenario_losses", I64 * 16), ("group_weights", I64 * 16),
    *[(name, I64) for name in ("liquid_value", "turnover", "expected_income", "execution_cost",
        "expected_net_income", "worst_stress_loss")], ("worst_scenario", U32)])
CandidateComparison = structure("CandidateComparison", [
    ("plans", AllocationScore * 3), ("count", U32), ("examined", U32), ("rejected", U32), ("complete", BOOL)])
GridRequest = structure("GridRequest", [("step_bps", I64), ("maximum_nodes", U64)])
GridSearch = structure("GridSearch", [
    ("comparison", CandidateComparison), ("nodes", U64), ("scored", U64), ("step_bps", I64)])
AllocationRequest = structure("AllocationRequest", [
    ("products", Product * 16), ("count", U32), ("policy", AllocationPolicy), ("grid", GridRequest)])
Descriptor = structure("Descriptor", [("operation", U32), ("version", U32), ("input_size", U64), ("output_size", U64)])
DepthLevel = structure("DepthLevel", [("price", I64), ("quantity", I64)])
VenueDepth = structure("VenueDepth", [
    ("venue", U64), ("instrument", U64), ("quote_asset", U64), ("base_asset", U64),
    ("levels", DepthLevel * 32), ("count", U32), ("side", U32),
    *[(name, I64) for name in ("taker_fee_bps", "fixed_cost", "quantity_step", "minimum_quantity",
        "minimum_notional", "maximum_notional")], ("envelope", Envelope)])
RoutePolicy = structure("RoutePolicy", [
    ("instrument", U64), ("base_asset", U64), ("quote_asset", U64), ("side", U32), ("order_type", U32),
    *[(name, I64) for name in ("quantity", "limit_price", "quote_budget", "maximum_slippage_bps",
        "reference_price")], ("maximum_venues", U32)])
SplitChild = structure("SplitChild", [
    ("venue", U64), *[(name, I64) for name in ("quantity", "limit_price", "notional", "fee",
        "fixed_cost", "cash_flow")], ("sequence", U64), ("valid_until_ns", U64)])
SplitPlan = structure("SplitPlan", [
    ("children", SplitChild * 16), ("count", U32),
    *[(name, I64) for name in ("requested_quantity", "filled_quantity", "unfilled_quantity", "total_notional",
        "total_fees", "fixed_costs", "cash_flow")], ("valid_until_ns", U64), ("execution_authority", U64)])
VenueRouteRequest = structure("VenueRouteRequest", [
    ("venues", VenueDepth * 16), ("count", U32), ("policy", RoutePolicy)])
WeightedScenario = structure("WeightedScenario", [("id", U64), ("loss", I64), ("probability_bps", U32)])
TailRisk = structure("TailRisk", [
    *[(name, I64) for name in ("expected_loss", "value_at_risk", "conditional_value_at_risk",
        "maximum_loss", "minimum_loss")], *[(name, U32) for name in (
        "confidence_bps", "tail_probability_bps", "tail_scenarios")]])
TailRiskRequest = structure("TailRiskRequest", [
    ("scenarios", WeightedScenario * 256), ("count", U32), ("confidence_bps", U32)])
PriceSample = structure("PriceSample", [("timestamp_ns", U64), ("price", I64)])
ReturnStatistics = structure("ReturnStatistics", [
    *[(name, I64) for name in ("total_return", "average_simple_return", "variance", "standard_deviation",
        "maximum_drawdown", "minimum_price", "maximum_price", "time_weighted_price")],
    ("duration_ns", U64), ("intervals", U32)])
ReturnStatisticsRequest = structure("ReturnStatisticsRequest", [
    ("samples", PriceSample * 256), ("count", U32), ("expected_interval_ns", U64), ("interval_tolerance_ns", U64)])
FactorExposure = structure("FactorExposure", [("asset", U64), ("signed_value", I64), ("loadings", I64 * 16)])
FactorBound = structure("FactorBound", [
    ("factor", U64), *[(name, I64) for name in ("maximum_gross", "minimum_net", "maximum_net")]])
FactorRisk = structure("FactorRisk", [("gross", I64 * 16), ("net", I64 * 16), ("count", U32), ("violated", U32)])
FactorRiskRequest = structure("FactorRiskRequest", [
    ("assets", FactorExposure * 32), ("bounds", FactorBound * 16), ("asset_count", U32), ("factor_count", U32)])
EconomicIdentity = structure("EconomicIdentity", [(name, U64) for name in (
    "process", "program", "program_version", "policy", "policy_version", "account", "network", "asset")])
CapitalInvariant = structure("CapitalInvariant", [(name, I64) for name in (
    "ceiling", "reserved", "spent", "max_single", "max_fee", "max_loss", "exposure_limit", "immediate_cash_floor")])
ActionEnvelope = structure("ActionEnvelope", [
    ("action", U32), *[(name, U64) for name in (
        "intent", "plan_commitment", "state_commitment", "destination", "expires_ns")],
    *[(name, I64) for name in ("amount", "maximum_cost", "maximum_loss", "exposure_after", "immediate_cash_after")],
    ("destination_allowed", BOOL)])
SimulationEvidence = structure("SimulationEvidence", [
    *[(name, U64) for name in ("intent", "plan_commitment", "state_commitment", "evidence_commitment", "observed_ns")],
    *[(name, I64) for name in ("projected_amount", "projected_cost", "projected_loss", "projected_exposure", "projected_cash")],
    ("adapter_verified", BOOL), ("successful", BOOL)])
AuthorizationEvidence = structure("AuthorizationEvidence", [
    *[(name, U64) for name in ("authorization", "intent", "plan_commitment", "account", "network",
        "policy_version", "authorized_ns", "expires_ns")], ("externally_verified", BOOL)])
SettlementEvidence = structure("SettlementEvidence", [
    *[(name, U64) for name in ("intent", "transaction_commitment", "block_commitment", "block_height",
        "finalized_height", "source_a", "source_b", "post_state_commitment")],
    *[(name, I64) for name in ("actual_amount", "actual_cost", "actual_loss", "actual_exposure", "actual_cash")],
    ("successful", BOOL), ("independent_sources_agree", BOOL), ("adapter_verified", BOOL)])
TransitionInput = structure("TransitionInput", [
    ("event", U64), ("fingerprint", U64), ("kind", U32), ("identity", EconomicIdentity), ("state", Envelope),
    ("action", ActionEnvelope), ("simulation", SimulationEvidence), ("authorization", AuthorizationEvidence),
    ("settlement", SettlementEvidence)])
EconomicProcess = structure("EconomicProcess", [
    ("identity", EconomicIdentity), ("phase", U32), ("capital", CapitalInvariant), ("action", ActionEnvelope),
    ("simulation", SimulationEvidence), ("authorization", AuthorizationEvidence), ("settlement", SettlementEvidence),
    *[(name, U64) for name in ("sequence", "last_event", "last_fingerprint", "last_observed_ns")],
    ("intent_reserved", I64), ("policy_paused", BOOL), ("needs_resolver", BOOL)])
TransitionReceipt = structure("TransitionReceipt", [
    ("error", U32), ("before", U32), ("after", U32), ("event", U64), ("sequence", U64),
    ("capital_held", I64), ("capital_spent", I64), ("diagnostic_fingerprint", U64), ("replay", BOOL),
    ("execution_authority", U64)])
TransitionRequest = structure("TransitionRequest", [("process", EconomicProcess), ("event", TransitionInput)])
TransitionOutput = structure("TransitionOutput", [("process", EconomicProcess), ("receipt", TransitionReceipt)])
FactKey = structure("FactKey", [(name, U64) for name in ("account", "network", "domain", "instrument", "field")])
TypedFact = structure("TypedFact", [("key", FactKey), ("unit", U32), ("asset", U64), ("quote_asset", U64),
    ("value", I64), *[(name, U64) for name in (
        "source", "source_generation", "sequence", "observed_ns", "valid_for_ns")], ("status", U32)])
FactDelta = structure("FactDelta", [("event", U64), ("fingerprint", U64), ("operation", U32), ("fact", TypedFact)])
Dependency = structure("Dependency", [("key", FactKey), ("unit", U32), ("asset", U64), ("quote_asset", U64)])
DependencyFrame = structure("DependencyFrame", [("facts", TypedFact * 32), ("count", U32),
    *[(name, U64) for name in ("state_generation", "valid_until_ns", "newest_ns", "oldest_ns")]])
StateFrameRequest = structure("StateFrameRequest", [("deltas", FactDelta * 64), ("dependencies", Dependency * 32),
    ("delta_count", U32), ("dependency_count", U32), ("now_ns", U64), ("maximum_skew_ns", U64)])
RegisterType = structure("RegisterType", [("unit", U32), ("asset", U64), ("quote_asset", U64)])
StrategyInstruction = structure("StrategyInstruction", [("opcode", U32), ("destination", U32), ("a", U32),
    ("b", U32), ("c", U32), ("immediate", I64), ("type", RegisterType), ("true_target", U32), ("false_target", U32)])
StrategyProgram = structure("StrategyProgram", [("id", U64), ("version", U64), ("policy_version", U64),
    ("dependency_types", RegisterType * 32), ("dependency_count", U32),
    ("instructions", StrategyInstruction * 128), ("count", U32), ("ttl_ns", U64)])
StrategyFrame = structure("StrategyFrame", [("dependencies", DependencyFrame), ("program_version", U64),
    ("policy_version", U64), ("now_ns", U64), ("policy_active", BOOL)])
StrategyRequest = structure("StrategyRequest", [("program", StrategyProgram), ("frame", StrategyFrame)])
StrategyResult = structure("StrategyResult", [("error", U32), ("action", U32), ("instruction", U32),
    ("executed", U32), ("amount", I64), ("score", I64), *[(name, U64) for name in (
        "program", "program_version", "state_generation", "valid_until_ns", "diagnostic_fingerprint", "execution_authority")]])
ExecutionAsset = structure("ExecutionAsset", [("asset", U64), *[(name, I64) for name in (
    "available", "reserved", "liability", "price_low", "price_high", "collateral_factor", "liquidation_factor", "cash_floor")]])
ExecutionStep = structure("ExecutionStep", [("id", U64), ("primitive", U32), *[(name, U64) for name in (
    "account", "network", "protocol", "destination", "dependencies", "asset_in", "asset_out", "fee_asset", "expires_ns")],
    *[(name, I64) for name in ("debit", "minimum_credit", "maximum_credit", "maximum_fee", "maximum_cost_value",
        "quantity_step", "assert_minimum", "assert_maximum")], ("destination_allowed", BOOL), ("quote_verified", BOOL)])
ExecutionTarget = structure("ExecutionTarget", [("asset", U64), *[(name, I64) for name in (
    "minimum_available", "maximum_available", "minimum_reserved", "maximum_reserved", "maximum_liability")]])
ExecutionGraph = structure("ExecutionGraph", [*[(name, U64) for name in (
    "plan", "account", "network", "policy_version", "expected_policy_version", "state_generation",
    "now_ns", "observed_ns", "max_age_ns", "expires_ns")],
    ("assets", ExecutionAsset * 32), ("steps", ExecutionStep * 64), ("asset_count", U32), ("step_count", U32),
    ("allowed_protocols", U64 * 16), ("protocol_count", U32), *[(name, I64) for name in (
        "maximum_cost_value", "maximum_turnover_value", "maximum_debt_value", "minimum_health_factor")],
    ("targets", ExecutionTarget * 32), ("target_count", U32)])
ExecutionProjection = structure("ExecutionProjection", [("plan", U64), ("order", U32 * 64),
    ("assets_after", ExecutionAsset * 32), ("count", U32), ("asset_count", U32), *[(name, I64) for name in (
        "total_cost_value", "turnover_value", "liability_value", "liquidation_capacity", "borrow_capacity", "health_factor")],
    ("valid_until_ns", U64), ("diagnostic_fingerprint", U64), ("debt_free", BOOL), ("execution_authority", U64)])

OPERATIONS = {
    "ORACLE_PRICE": (1, OracleRequest, OraclePrice),
    "LENDING_RATES": (2, LendingMarket, LendingRates),
    "FORWARD_YIELD": (3, YieldTerms, YieldEstimate),
    "LENDING_HEALTH": (4, HealthRequest, Health),
    "DERIVATIVE_RISK": (5, LinearPosition, PositionRisk),
    "FUNDING_CASHFLOW": (6, FundingRequest, FundingCashFlow),
    "DERIVATIVE_RECOVERY": (7, DerivativeRecoveryRequest, RecoveryChoice),
    "REPAYMENT_DECISION": (8, RepaymentRequest, RepayChoice),
    "REBALANCE_DECISION": (9, RebalanceRequest, RebalanceChoice),
    "AMM_SWAP": (10, SwapRequest, SwapQuote),
    "AMM_ROUTE": (11, SwapRoute, RouteQuote),
    "ALLOCATION_SEARCH": (12, AllocationRequest, GridSearch),
    "VENUE_ROUTE": (13, VenueRouteRequest, SplitPlan),
    "SCENARIO_TAIL_RISK": (14, TailRiskRequest, TailRisk),
    "RETURN_STATISTICS": (15, ReturnStatisticsRequest, ReturnStatistics),
    "FACTOR_RISK": (16, FactorRiskRequest, FactorRisk),
    "STATE_TRANSITION": (17, TransitionRequest, TransitionOutput),
    "STATE_FRAME": (18, StateFrameRequest, DependencyFrame),
    "ECONOMIC_PROGRAM": (19, StrategyRequest, StrategyResult),
    "EXECUTION_GRAPH": (20, ExecutionGraph, ExecutionProjection),
}
ERRORS = ("OKAY", "INVALID", "OVERFLOW", "DIVIDE_ZERO", "CAPACITY", "MISSING", "STALE", "FUTURE",
          "SEQUENCE", "CONFLICT", "UNSUPPORTED", "UNAUTHORIZED", "EXPIRED", "ILLIQUID", "CONCENTRATION",
          "LEVERAGE", "COLLATERAL", "COST", "NO_IMPROVEMENT", "INFEASIBLE", "UNKNOWN", "DISPERSION",
          "QUORUM", "DOMAIN", "REORG", "BACKPRESSURE", "INCOMPLETE")
ACTIONS = ("NONE", "HOLD", "REDUCE", "HEDGE", "REPAY", "BORROW", "ALLOCATE", "SWAP", "CANCEL", "ESCALATE", "ABORT")


def marshal(kind, raw):
    if issubclass(kind, c.Structure):
        require_keys(raw, {name for name, _ in kind._fields_}, kind.__name__)
        value = kind()
        for name, child in kind._fields_:
            setattr(value, name, marshal(child, raw[name]))
        return value
    if issubclass(kind, c.Array):
        if not isinstance(raw, list) or len(raw) > kind._length_:
            raise MachineError("ECONOMIC_ARRAY_BOUND")
        value = kind()
        for index, entry in enumerate(raw):
            value[index] = marshal(kind._type_, entry)
        return value
    if kind is BOOL:
        if type(raw) is not bool:
            raise MachineError("ECONOMIC_BOOLEAN_REQUIRED")
        return raw
    if isinstance(raw, dict):
        require_keys(raw, {"integer"}, "lossless native integer")
        literal = raw["integer"]
        if not isinstance(literal, str) or not re.fullmatch(r"(?:0|-?[1-9][0-9]{0,19})", literal):
            raise MachineError("ECONOMIC_INTEGER_LITERAL_REQUIRED")
        raw = int(literal)
    if type(raw) is not int:
        raise MachineError("ECONOMIC_EXACT_INTEGER_REQUIRED")
    low, high = (-(2**63), 2**63 - 1) if kind is I64 else (0, 2**(c.sizeof(kind) * 8) - 1)
    if not low <= raw <= high:
        raise MachineError("ECONOMIC_INTEGER_RANGE")
    return raw


def extract(value):
    if isinstance(value, c.Structure):
        return {name: extract(getattr(value, name)) for name, _ in value._fields_}
    if isinstance(value, c.Array):
        return [extract(child) for child in value]
    if type(value) is int and abs(value) > 2**53 - 1:
        # Browser JSON numbers cannot exactly preserve arbitrary 64-bit IDs,
        # micro-amounts or fingerprints. The explicit typed literal is also
        # accepted on input; untagged numeric strings remain invalid.
        return {"integer": str(value)}
    return value


def schema(kind):
    if issubclass(kind, c.Structure):
        return {name: schema(child) for name, child in kind._fields_}
    if issubclass(kind, c.Array):
        return {"maximum_items": kind._length_, "item": schema(kind._type_)}
    if kind is BOOL:
        return "boolean"
    return "int64" if kind is I64 else "uint64" if kind is U64 else "uint32"


class EconomicLibrary:
    def __init__(self, workspace, path=None, expected_hash=None):
        self.work, self.library, self.sha256 = workspace, None, None
        location = path or os.environ.get("ENGINE_ECONOMICS_LIBRARY")
        checksum = expected_hash or os.environ.get("ENGINE_ECONOMICS_SHA256")
        if not location:
            return
        candidate = Path(location)
        if (not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file()
                or candidate.stat().st_size > 20_000_000):
            raise MachineError("TRUSTED_ECONOMIC_LIBRARY_REQUIRED")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if not checksum or actual != checksum:
            raise MachineError("ECONOMIC_LIBRARY_HASH_MISMATCH")
        library = c.CDLL(str(candidate))
        library.machine_economics_abi.restype = U32
        library.machine_economics_descriptor.argtypes = [U32, c.POINTER(Descriptor)]
        library.machine_economics_descriptor.restype = c.c_int
        library.machine_economics_evaluate.argtypes = [U32, c.c_void_p, c.c_size_t, c.c_void_p, c.c_size_t]
        library.machine_economics_evaluate.restype = c.c_int
        if library.machine_economics_abi() != 1:
            raise MachineError("ECONOMIC_ABI_MISMATCH")
        for operation, input_type, output_type in OPERATIONS.values():
            description = Descriptor()
            if (library.machine_economics_descriptor(operation, c.byref(description)) != 0
                    or description.version != 1 or description.operation != operation
                    or description.input_size != c.sizeof(input_type) or description.output_size != c.sizeof(output_type)):
                raise MachineError("ECONOMIC_DOMAIN_ABI_MISMATCH")
        self.library, self.sha256 = library, actual

    def status(self):
        return {"enabled": self.library is not None, "abi": 1, "library_sha256": self.sha256,
                "operations": list(OPERATIONS), "execution_authority": "NONE",
                "input_assurance": "CALLER_SUPPLIED_ASSUMPTIONS_NOT_AUTHENTICATED_MARKET_STATE",
                "language_model_calls": 0, "general_intelligence": False}

    def catalogue(self):
        return self.status() | {"schemas": {name: {"input": schema(inp), "output": schema(out)}
                                           for name, (_, inp, out) in OPERATIONS.items()},
                               "numeric_scale": 1000000, "basis_points_scale": 10000,
                               "lossless_integer_encoding": {"integer": "canonical signed decimal string"},
                               "errors": list(ERRORS), "actions": list(ACTIONS)}

    def validate_request(self, raw):
        require_keys(raw, {"operation", "input"}, "native economic task")
        if not isinstance(raw["operation"], str) or raw["operation"] not in OPERATIONS:
            raise MachineError("SUPPORTED_ECONOMIC_OPERATION_REQUIRED")
        if len(canonical(raw)) > 32000:
            raise MachineError("ECONOMIC_TASK_SIZE_BOUND")
        number, input_type, output_type = OPERATIONS[raw["operation"]]
        return number, marshal(input_type, raw["input"]), output_type

    def calculate(self, raw):
        if self.library is None:
            raise MachineError("ECONOMIC_LIBRARY_NOT_CONFIGURED")
        number, request, output_type = self.validate_request(raw)
        output = output_type()
        code = self.library.machine_economics_evaluate(number, c.byref(request), c.sizeof(request),
                                                       c.byref(output), c.sizeof(output))
        if not 0 <= code < len(ERRORS):
            raise MachineError("ECONOMIC_NATIVE_RESULT_INVALID")
        result = extract(output) if code == 0 else None
        if result and result.get("execution_authority", 0) != 0:
            raise MachineError("ECONOMIC_AUTHORITY_BOUNDARY_FAILED")
        body = {"operation": raw["operation"], "computed": code == 0, "code": code,
                "reason": ERRORS[code], "result": result, "input_sha256": digest(raw),
                "library_sha256": self.sha256, "abi": 1, "execution_authority": "NONE",
                "input_assurance": "CALLER_SUPPLIED_ASSUMPTIONS_NOT_AUTHENTICATED_MARKET_STATE",
                "language_model_calls": 0, "observed_at": int(self.work.clock() if self.work else time.time())}
        if result and "action" in result:
            action = result["action"]
            if type(action) is not int or not 0 <= action < len(ACTIONS):
                raise MachineError("ECONOMIC_ACTION_INVALID")
            body["action"] = ACTIONS[action]
        if self.work is None:
            body["demonstration"] = "SYNTHETIC_ASSUMPTIONS_NOT_CUSTOMER_ACCOUNT"
        body["receipt_sha256"] = digest(body)
        return body

    def evaluate(self, raw):
        if self.work is None:
            raise MachineError("ECONOMIC_WORKSPACE_REQUIRED")
        body = self.calculate(raw)
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.work.event(db, "NATIVE_ECONOMIC_DECISION", body)
        return body
