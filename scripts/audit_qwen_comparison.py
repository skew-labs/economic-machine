"""Independently recalculate outcomes from precommitted policies and raw records.

Does not import the engine's negotiate/normalize/usage helpers. This verifies
internal evidence consistency, not an external auditor or provider invoice.
"""

import argparse
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path


def atoms(value):
    precise = Decimal(value) * 1_000_000
    assert precise == int(precise), "money must have at most six decimals"
    return int(precise)


def allowed(policy, item, now):
    supply = item["raw"]
    price = atoms(supply["unit_price"])
    if policy["units"] >= supply["discount_min_units"]:
        price = price * (10000 - supply["discount_bps"]) // 10000
    price = max(price, atoms(supply["floor_price"]))
    valid = all([policy["payment_asset"] == supply["payment_asset"],
        policy["data_type"] == supply["data_type"], policy["purpose"] in supply["purposes"],
        policy["license"] in supply["licenses"], supply["min_units"] <= policy["units"] <= supply["max_units"],
        0 <= now - supply["updated_at"] < policy["max_age_seconds"],
        supply["refresh_seconds"] <= policy["max_refresh_seconds"],
        supply["response_seconds"] <= policy["response_seconds"],
        price <= atoms(policy["max_unit_price"]), price * policy["units"] <= atoms(policy["max_total_price"])])
    return valid, price * policy["units"]


def usage(calls):
    result = {"calls": len(calls)}
    for field in ["prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"]:
        values = [call["usage"].get(field) for call in calls]
        result[field] = (None if any(value is None for value in values)
                         else str(sum((Decimal(v) for v in values), Decimal(0))) if field == "cost_usd"
                         else sum(values))
    return result


def audit(directory):
    protocol = json.loads((directory / "protocol.json").read_text())
    result = json.loads((directory / "result.json").read_text())
    canonical = json.dumps(protocol, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    protocol_hash = hashlib.sha256(canonical).hexdigest()
    assert protocol_hash == result["protocol_hash"] == (directory / "protocol.sha256").read_text().strip()
    assert protocol == result["protocol"]
    for name, expected in protocol["sources"].items():
        actual = hashlib.sha256((directory / "source_snapshot" / name).read_bytes()).hexdigest()
        assert actual == expected, "source snapshot mismatch"
    calls = [json.loads(line) for line in (directory / "provider-calls.jsonl").read_text().splitlines()]
    rows = [json.loads(line) for line in (directory / "trades.jsonl").read_text().splitlines()]
    assert rows == result["records"]
    assert len(calls) <= protocol["max_calls"]
    assert [call["ordinal"] for call in calls] == list(range(len(calls)))
    assert calls[0]["purpose"] == "shared_policy_compiler"
    assert all(call.get("returned_model") == protocol["model"] for call in calls)
    assert all(call["started_at"] >= protocol["created_at"] for call in calls)
    assert len({call["provider_id"] for call in calls}) == len(calls), "provider IDs must be distinct"
    for call in calls:
        metering = call["usage"]
        assert all(type(metering[field]) is int and metering[field] >= 0
                   for field in ["prompt_tokens", "completion_tokens", "total_tokens"])
        assert metering["prompt_tokens"] + metering["completion_tokens"] == metering["total_tokens"]
    assert usage(calls) == {key: result["all_actual_provider_usage"][key] for key in usage(calls)}
    policy = calls[0]["final_json"]
    expected = protocol["expected_compiled_policy"]
    assert set(policy) == set(expected)
    assert all((atoms(policy[key]) == atoms(value) if key in {"max_unit_price", "max_total_price"}
                else policy[key] == value) for key, value in expected.items())
    assert len(rows) == 3 * len(protocol["events"])
    paid = {(r["arm"], r["trade"]): r for r in result["settlements"]}
    assert len(paid) == len(result["settlements"])
    referenced = []
    counts = {arm: {"eligible": 0, "primary_success": 0} for arm in ["direct_qwen", "direct_code", "machine"]}
    for event in protocol["events"]:
        group = [row for row in rows if row["trade"] == event["id"]]
        assert len(group) == 3 and {r["arm"] for r in group} == set(counts)
        raw = {**policy, "data_type": event["data_type"]}
        options = [item for item in protocol["supplier_book"] if allowed(raw, item, protocol["created_at"])[0]]
        optimal = min(options, key=lambda item: (allowed(raw, item, protocol["created_at"])[1], item["raw"]["version"])) if options else None
        for row in group:
            choice = next((item for item in protocol["supplier_book"] if item["id"] == row["choice_id"]), None)
            valid = choice is not None and allowed(raw, choice, protocol["created_at"])[0]
            assert valid == row["valid_agreement"]
            assert bool(options) == row["eligible"]
            assert row["expected_id"] == (optimal["id"] if optimal else None)
            decision_ok = valid and row["elapsed_ms"] <= protocol["primary_decision_deadline_seconds"] * 1000
            assert decision_ok == row["timely_primary_agreement"]
            key = (row["arm"], row["trade"])
            assert (key in paid) == decision_ok == row["settlement_verified"]
            if key in paid:
                settlement = paid[key]
                assert settlement["verified"] and settlement["public_chain_payment"] is False
                assert settlement["signed_submissions"] == 1 and settlement["reserved_atoms"] == 0
                assert settlement["recipient_delta_atoms"] == settlement["spent_atoms"] == 400000
                assert settlement["payment"]["status"] == "SETTLED"
                assert settlement["payment"]["observation"]["status"] == "PAID"
                assert settlement["payment"]["tx_hash"] == row["tx_hash"]
            counts[row["arm"]]["eligible"] += bool(options)
            counts[row["arm"]]["primary_success"] += decision_ok
            referenced.extend(row["model_call_ordinals"])
            if row["arm"] != "direct_qwen":
                assert not row["model_call_ordinals"]
    assert sorted(referenced) == list(range(1, len(calls))), "every billed attempt must be allocated exactly once"
    for arm, count in counts.items():
        summary = result["summaries"][arm]
        assert count["eligible"] == summary["eligible_events"]
        assert count["primary_success"] == summary["primary_completed_orders"]
        arm_calls = [calls[index] for row in rows if row["arm"] == arm for index in row["model_call_ordinals"]]
        assert usage([calls[0], *arm_calls]) == {key: summary["including_shared_policy_compile"][key]
                                               for key in usage([calls[0], *arm_calls])}
    assert len({row["payment"]["tx_hash"] for row in paid.values()}) == len(paid)
    balances = result["test_token_balances"]
    assert balances["recipient_atoms"] == len(paid) * 400000
    assert balances["payer_atoms"] + balances["recipient_atoms"] == balances["initial_payer_atoms"]
    if "observed_http_requests" in result:
        observed = result["observed_http_requests"]
        assert observed["/api/supplies"] == len(protocol["supplier_book"])
        assert observed["/api/demands"] == len(protocol["events"])
        assert sum(value for key, value in observed.items() if key.startswith("/benchmark/suppliers/")) == 16 * len(protocol["events"])
    for path in ["result.json", "protocol.json", "provider-calls.jsonl", "trades.jsonl"]:
        content = (directory / path).read_text()
        assert "sk-bk-" not in content and '"Authorization"' not in content and '"signature":' not in content
    return {"passed": True, "protocol_hash": protocol_hash, "source_snapshots": len(protocol["sources"]),
        "independent_policy_predicate": True, "provider_calls_verified": len(calls), "records_verified": len(rows),
        "unique_test_token_payments": len(paid), "all_payment_balances_reconciled": True,
        "counts": counts, "public_customer_conversion_proven": False}


def matching_stores(directory):
    """Check actual HTTP engine agreements against downstream payment-store terms.

    Buyer/seller identities differ in isolated fixtures; economic fields must
    match exactly. Read the remote stores, which are intentionally not exported.
    """
    result = json.loads((directory / "result.json").read_text())
    machine = sorted((row for row in result["records"] if row["arm"] == "machine"), key=lambda r: r["trade"])
    checked = 0
    with sqlite3.connect(f"file:{directory / 'market.sqlite3'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        demands = db.execute("SELECT id FROM demands ORDER BY rowid").fetchall()
        assert len(demands) == len(machine)
        for demand, row in zip(demands, machine, strict=True):
            bodies = [json.loads(r[0]) for r in db.execute("SELECT body FROM matches WHERE demand_id=?", (demand["id"],))]
            assert bool(bodies) == row["eligible"]
            if not row["settlement_verified"]:
                continue
            choice = next(item for item in result["protocol"]["supplier_book"] if item["id"] == row["choice_id"])
            agreed = next(body["terms"] for body in bodies if body["terms"]["data_version"] == choice["raw"]["version"])
            # Payment fixtures have their own owners/record IDs, not the public
            # API test sessions. Exclude only those four identity fields.
            economic = {key: value for key, value in agreed.items()
                        if key not in {"buyer_id", "seller_id", "demand_id", "supply_id"}}
            with sqlite3.connect(f"file:{directory / ('machine-' + str(row['trade']) + '.sqlite3')}?mode=ro", uri=True) as paid:
                terms = json.loads(paid.execute("SELECT body FROM matches").fetchone()[0])["terms"]
                assert economic == {key: value for key, value in terms.items()
                                    if key not in {"buyer_id", "seller_id", "demand_id", "supply_id"}}
            checked += 1
    return {"http_engine_to_payment_economic_terms_verified": checked,
            "isolation_identity_fields_excluded": ["buyer_id", "seller_id", "demand_id", "supply_id"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--verify-stores", action="store_true")
    args = parser.parse_args()
    result = audit(args.directory)
    if args.verify_stores:
        result.update(matching_stores(args.directory))
    (args.directory / "audit.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
