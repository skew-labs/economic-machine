"""Paired HTTP benchmark: direct scan, indexed direct clients, Economic Machine.

No injected latency or fictitious model tokens. All arms see the same book and
use the same economic predicates. Initial indexing/registration cost is reported.
This is a controlled localhost workload, not a public-market conversion study.
"""

import argparse
import asyncio
import json
import platform
import random
import socket
import statistics
import threading
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import httpx
import uvicorn

from economic_machine.values import digest
from machine_commerce.api import create_app
from machine_commerce.market import negotiate, normalize_demand, normalize_supply

ROOT = Path(__file__).resolve().parents[1]


def book(now, count):
    return [{"name": f"Supplier {i}", "data_type": f"data.type-{i % 8}", "version": f"version-{i}",
        "unit_price": "0.05", "floor_price": "0.035", "discount_bps": 2000, "discount_min_units": 10,
        "min_units": 1, "max_units": 100, "purposes": ["research"], "licenses": ["internal-use"],
        "updated_at": now, "refresh_seconds": 30, "response_seconds": 10, "ttl_seconds": 3600}
        for i in range(count)]


def buyer(i):
    return {"data_type": f"data.type-{i % 8}", "purpose": "research", "license": "internal-use",
        "units": 10, "max_unit_price": "0.03" if i % 4 == 0 else "0.04", "max_total_price": "0.4",
        "max_age_seconds": 3600, "max_refresh_seconds": 60, "response_seconds": 30, "ttl_seconds": 3600}


def fingerprint(result):
    if result is None:
        return None
    terms = {k: v for k, v in result["terms"].items()
             if k not in {"buyer_id", "seller_id", "demand_id", "supply_id"}}
    return digest(terms)


def select(results):
    eligible = [r for r in results if r["status"] == "AGREED"]
    return min(eligible, key=lambda r: (Decimal(r["terms"]["total_price"]), r["terms"]["data_version"])) if eligible else None


def quantile(values, q):
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * q)]


def summarize(rows):
    samples = [r["elapsed_ms"] for r in rows]
    eligible = sum(r["ground_truth_eligible"] for r in rows)
    completed = sum(r["valid_agreement"] for r in rows)
    return {"trades": len(rows), "discovery_requests": sum(r["requests"]["discovery"] for r in rows),
        "negotiation_requests": sum(r["requests"]["negotiation"] for r in rows),
        "total_trade_requests": sum(sum(r["requests"].values()) for r in rows),
        "agreement_ms": {"p50": statistics.median(samples), "p95": quantile(samples, .95)},
        "valid_agreements": completed, "eligible_trades": eligible,
        "agreement_rate": completed / len(rows), "eligible_completion_rate": completed / eligible if eligible else None,
        "wrong_agreements": sum(r["incorrect"] for r in rows), "llm_calls": 0,
        "input_tokens": 0, "output_tokens": 0, "observed_llm_cost_usd": "0"}


async def compare(base, supplies, count, seed):
    rng = random.Random(seed)
    records, setup = [], {}
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits) as client:
        response = await client.post("/api/sessions", json={})
        response.raise_for_status()
        # The owner token is deliberately not included in any record.
        start = time.perf_counter()
        for raw in supplies:
            result = await client.post("/api/supplies", json=raw)
            result.raise_for_status()
        setup["machine"] = {"requests": len(supplies) + 1, "elapsed_ms": (time.perf_counter() - start) * 1000,
                            "purpose": "one-time seller registration; includes owner session request count"}
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits) as client:
        await client.post("/api/sessions", json={})
        started = time.perf_counter()
        cached = (await client.get("/benchmark/directory")).json()["suppliers"]
        setup["direct_cached"] = {"requests": 1, "elapsed_ms": (time.perf_counter() - started) * 1000,
                                  "purpose": "one-time public data-type directory cache"}
        setup["direct_scan"] = {"requests": 0, "elapsed_ms": 0}
        for trade in range(count):
            raw = buyer(trade)
            now = int(time.time())
            truth = select([negotiate(normalize_demand(raw), normalize_supply(s, now), now) for s in supplies])
            arms = ["direct_scan", "direct_cached", "machine"]
            rng.shuffle(arms)
            for arm in arms:
                started = time.perf_counter()
                requests = Counter()
                if arm == "machine":
                    requests["registration"] += 1
                    response = await client.post("/api/demands", json=raw)
                    response.raise_for_status()
                    chosen = select(response.json()["matches"])
                else:
                    if arm == "direct_scan":
                        requests["discovery"] += 1
                        directory = (await client.get("/benchmark/directory")).json()["suppliers"]
                    else:
                        directory = [s for s in cached if s["data_type"] == raw["data_type"]]
                    # Eight concurrent requests is shared across both direct baselines.
                    semaphore = asyncio.Semaphore(8)

                    async def quote(item, semaphore=semaphore, requests=requests, raw=raw):
                        async with semaphore:
                            requests["negotiation"] += 1
                            response = await client.post(f'/benchmark/suppliers/{item["id"]}/quote', json=raw)
                            response.raise_for_status()
                            return response.json()
                    chosen = select(await asyncio.gather(*(quote(item) for item in directory)))
                elapsed = (time.perf_counter() - started) * 1000
                same = fingerprint(chosen) == fingerprint(truth)
                records.append({"trade": trade, "arm": arm, "input_hash": digest(raw), "elapsed_ms": elapsed,
                    "requests": {k: requests[k] for k in ["discovery", "negotiation", "registration", "agreement_read"]},
                    "ground_truth_eligible": truth is not None, "valid_agreement": chosen is not None and same,
                    "incorrect": not same, "result_hash": fingerprint(chosen), "expected_hash": fingerprint(truth)})
    return records, setup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suppliers", type=int, default=64)
    parser.add_argument("--trades", type=int, default=24)
    parser.add_argument("--seed", type=int, default=20261002)
    args = parser.parse_args()
    if not 8 <= args.suppliers <= 100 or not 8 <= args.trades <= 200:
        parser.error("bounded benchmark requires 8–100 suppliers and 8–200 trades")
    run = ROOT / "artifacts" / f"comparison-{int(time.time())}"
    run.mkdir(parents=True)
    now = int(time.time())
    supplies = book(now, args.suppliers)
    app = create_app(run / "state.sqlite3")
    observed = Counter()

    @app.middleware("http")
    async def count_request(request, call_next):
        observed[request.url.path] += 1
        return await call_next(request)

    @app.get("/benchmark/directory")
    def directory():
        return {"suppliers": [{"id": str(i), "data_type": s["data_type"]} for i, s in enumerate(supplies)]}

    @app.post("/benchmark/suppliers/{supplier_id}/quote")
    def quote(supplier_id: int, raw: dict):
        stamp = int(time.time())
        return negotiate(normalize_demand(raw), normalize_supply(supplies[supplier_id], stamp), stamp)

    # Bind a kernel-assigned localhost port; do not alter the running product service.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    if not server.started:
        raise RuntimeError("isolated benchmark server did not start")
    try:
        records, setup = asyncio.run(compare(base, supplies, args.trades, args.seed))
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
    summaries = {arm: summarize([r for r in records if r["arm"] == arm])
                 for arm in ["direct_scan", "direct_cached", "machine"]}
    for arm, summary in summaries.items():
        summary["requests_including_setup"] = summary["total_trade_requests"] + setup[arm]["requests"]
        summary["elapsed_including_setup_ms"] = setup[arm]["elapsed_ms"] + sum(r["elapsed_ms"] for r in records if r["arm"] == arm)
    paired = {}
    for baseline in ["direct_scan", "direct_cached"]:
        ratios = []
        for trade in range(args.trades):
            row = {r["arm"]: r for r in records if r["trade"] == trade}
            ratios.append(row["machine"]["elapsed_ms"] / row[baseline]["elapsed_ms"])
        samples = [statistics.median(rng_sample) for rng_sample in
                   [random.Random(args.seed + i).choices(ratios, k=len(ratios)) for i in range(1000)]]
        paired[baseline] = {"median_machine_latency_ratio": statistics.median(ratios),
                            "bootstrap_95_interval": [quantile(samples, .025), quantile(samples, .975)]}
    result = {"schema": "paired-http-comparison-1", "host": platform.node(), "seed": args.seed,
        "suppliers": args.suppliers, "trades": args.trades, "max_direct_concurrency": 8,
        "transport": "real HTTP on localhost, no injected latency", "setup": setup,
        "summaries": summaries, "paired": paired, "records": records, "observed_http_requests": dict(observed),
        "workload": "fixed typed policies, mixed feasible/infeasible prices, identical supplier book",
        "not_measured": ["WAN performance", "external customer conversion", "live LLM baseline", "provider billing"],
        "token_conclusion": "All three code paths use zero LLM tokens; token-cost improvement is not demonstrated.",
        "registration_note": "Machine's one-time seller registration cost is reported, not excluded from the product's total cost."}
    (ROOT / "artifacts/comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    (run / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"summaries": summaries, "setup": setup, "paired": paired}, indent=2))
    if any(summary["wrong_agreements"] for summary in summaries.values()):
        raise RuntimeError("comparison found incorrect economic decisions")


if __name__ == "__main__":
    main()
