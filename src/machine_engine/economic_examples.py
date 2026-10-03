"""Small labelled assumptions for native calculations; never account records."""


def derivative_example():
    return {"operation": "DERIVATIVE_RECOVERY", "input": {
        "position": {"instrument": 1, "signed_quantity": 10000000, "entry_price": 100000000,
            "mark_price": 100000000, "collateral": 100000000, "accrued_funding": 0, "unpaid_fees": 0,
            "initial_margin_rate": 100000, "maintenance_margin_rate": 50000},
        "policy": {"minimum_equity": 10000000, "target_margin_headroom": 70000000,
            "maximum_leverage": 6000000, "maximum_close_quantity": 10000000,
            "maximum_total_cost": 10000000, "max_slippage_bps": 100, "maximum_close_fraction": 1000000,
            "maximum_lots_examined": 20, "deadline_ns": 900},
        "venues": [{"id": 1, "executable_price": 100000000, "available_quantity": 10000000,
            "quantity_step": 1000000, "fee_rate": 1000, "fixed_cost": 0, "min_notional": 1000000,
            "allowed": True, "envelope": envelope()}], "count": 1, "state": envelope()}}


def envelope():
    return {"sequence": 1, "expected_sequence": 1, "observed_ns": 100, "now_ns": 200,
            "max_age_ns": 1000, "deadline_ns": 1000, "policy_version": 1, "expected_policy_version": 1}


def strategy_example():
    """Compiled synthetic collateral threshold: observe, compare, branch, act."""
    money = {"unit": 3, "asset": 10, "quote_asset": 0}
    boolean = {"unit": 7, "asset": 0, "quote_asset": 0}
    quantity = {"unit": 1, "asset": 8, "quote_asset": 0}

    def instruction(opcode, destination, kind, **fields):
        return {"opcode": opcode, "destination": destination, "a": 0, "b": 0, "c": 0,
                "immediate": 0, "type": kind, "true_target": 0, "false_target": 0} | fields

    fact = {"key": {"account": 1, "network": 421614, "domain": 2, "instrument": 3, "field": 4},
            "unit": 3, "asset": 10, "quote_asset": 0, "value": 100000000, "source": 20,
            "source_generation": 1, "sequence": 1, "observed_ns": 100, "valid_for_ns": 500, "status": 1}
    return {"operation": "ECONOMIC_PROGRAM", "input": {"program": {
        "id": 1, "version": 2, "policy_version": 3, "dependency_types": [money], "dependency_count": 1,
        "instructions": [instruction(1, 0, money), instruction(2, 1, money, immediate=150000000),
            instruction(12, 2, boolean, a=0, b=1, immediate=0),
            instruction(20, 3, boolean, a=2, true_target=4, false_target=6),
            instruction(2, 4, quantity, immediate=5000000), instruction(22, 5, boolean, a=4),
            instruction(21, 6, boolean)], "count": 7, "ttl_ns": 100},
        "frame": {"dependencies": {"facts": [fact], "count": 1, "state_generation": 1,
            "valid_until_ns": 600, "newest_ns": 100, "oldest_ns": 100},
            "program_version": 2, "policy_version": 3, "now_ns": 200, "policy_active": True}}}


def execution_example():
    """Two synthetic assets: swap first, then supply; no addresses/calldata."""
    def asset(identifier, available, price, collateral=0, liquidation=0, floor=0):
        return {"asset": identifier, "available": available, "reserved": 0, "liability": 0,
                "price_low": price, "price_high": price, "collateral_factor": collateral,
                "liquidation_factor": liquidation, "cash_floor": floor}

    def step(identifier, primitive, dependency, asset_in, asset_out, debit, minimum, maximum):
        return {"id": identifier, "primitive": primitive, "account": 7, "network": 421614,
            "protocol": 8, "destination": 9, "dependencies": dependency, "asset_in": asset_in,
            "asset_out": asset_out, "fee_asset": 0, "expires_ns": 500, "debit": debit,
            "minimum_credit": minimum, "maximum_credit": maximum, "maximum_fee": 0,
            "maximum_cost_value": 0, "quantity_step": 1000000, "assert_minimum": 0,
            "assert_maximum": 0, "destination_allowed": True, "quote_verified": True}

    return {"operation": "EXECUTION_GRAPH", "input": {"plan": 1, "account": 7, "network": 421614,
        "policy_version": 3, "expected_policy_version": 3, "state_generation": 4, "now_ns": 200,
        "observed_ns": 100, "max_age_ns": 500, "expires_ns": 600,
        "assets": [asset(10, 100000000, 1000000, floor=5000000),
                   asset(20, 0, 2000000, collateral=500000, liquidation=800000)],
        "steps": [step(200, 3, 2, 20, 20, 20000000, 20000000, 20000000),
                  step(100, 2, 0, 10, 20, 40000000, 20000000, 21000000)],
        "asset_count": 2, "step_count": 2, "allowed_protocols": [8], "protocol_count": 1,
        "maximum_cost_value": 5000000, "maximum_turnover_value": 100000000,
        "maximum_debt_value": 20000000, "minimum_health_factor": 1500000,
        "targets": [{"asset": 10, "minimum_available": 59000000, "maximum_available": 60000000,
            "minimum_reserved": 0, "maximum_reserved": 0, "maximum_liability": 0},
            {"asset": 20, "minimum_available": 0, "maximum_available": 0,
            "minimum_reserved": 20000000, "maximum_reserved": 20000000, "maximum_liability": 0}],
        "target_count": 2}}
