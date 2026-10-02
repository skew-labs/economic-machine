# Direct connections versus Economic Machine

This file preserves the original all-code study. The subsequent [real Qwen comparison](QWEN_COMPARISON.md)
measures actual provider tokens and paid test-token outcomes; its conclusions are specific to those model baselines.

The reproducible remote experiment is `scripts/compare_routes.py`; `artifacts/comparison.json` contains all
72 per-arm records, input/result hashes, request counters, setup costs and paired statistics.
The prior two-request implementation's result is retained in `comparison-before.json`.

64 suppliers span eight data categories. Each publishes the same typed rule schema. Buyers request ten units,
with a price cap of 0.04; every fourth request caps at 0.03 and is deliberately infeasible. Seller offer 0.05
less a 20% quantity discount becomes 0.04, above its 0.035 floor. Licenses, purpose, freshness and response
limits are identical across methods. The same deterministic economic predicate supplies the ground truth.

For each of 24 requests, a seeded random order selects the sequence of three arms:

- **Full scan:** refresh the directory, then query all 64 suppliers, at most eight concurrently.
- **Cached direct:** cache the public category directory once, then query eight matching suppliers concurrently.
- **Machine:** register seller policies once; POST a buyer demand and receive agreed terms in that response.

Each arm selects the lowest valid total price, breaking ties by data version. Result hashes exclude participant
IDs only. A different agreement from ground truth is an error. Each arm observes the same supplier book.
No latency is injected. The server is isolated from the running product and uses a new SQLite database in development mode; this is not a production authentication/load benchmark.
Client-issued requests are cross-checked against server-observed paths. Common fixture infrastructure and
one buyer authentication setup are shared; seller registration remains an explicitly charged machine setup cost.
The direct baseline does not store our audit/capital journal, so the comparison is not an implementation-equivalence claim.

| Metric | Full scan | Cached direct | Machine |
| --- | ---: | ---: | ---: |
| Discovery requests per trade | 1 | 0 | 0 |
| Quote requests per trade | 64 | 8 | 0 |
| Demand registration requests per trade | 0 | 0 | 1 |
| Total trade requests (24 requests) | 1,560 | 192 | 24 |
| One-time setup requests | 0 | 1 | 65 |
| Requests including measured setup | 1,560 | 193 | 89 |
| Agreement p50 | 417.12 ms | 67.35 ms | 54.03 ms |
| Agreement p95 | 450.17 ms | 76.33 ms | 60.58 ms |
| Measured setup time | 0 | 43.86 ms | 3,160.15 ms |
| Sum of measured trade+setup durations | 10,093.47 ms | 1,661.94 ms | 4,430.99 ms |
| Valid agreements / all requests | 18/24 | 18/24 | 18/24 |
| Feasible request completion | 18/18 | 18/18 | 18/18 |
| Incorrect agreements | 0 | 0 | 0 |
| LLM calls / input tokens / output tokens | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |

Against cached direct, warm trade request count falls 87.5%, including setup it falls 53.9% in this workload.
The paired median machine/direct agreement-latency ratio is 0.8013 (95% bootstrap interval 0.7553–0.8625).
Against full scan that ratio is 0.1299 (0.1254–0.1329). Intervals resample the 24 paired observations 1,000 times;
they are descriptive of this small fixed workload, not a production guarantee. Suppliers and trials are limited.

The first implementation was slower than cached direct because it repeated a full journal scan during one
exclusive matching transaction and required a second HTTP result request. It now verifies the journal once
per transaction and returns owned agreements with registration. The retained baseline prevents losing the initial negative result.

**Not proven:** lower token costs, higher customer conversion, higher agreement rate, WAN latency,
long-running capacity, dynamic inventory/freshness reliability or a paid trade's finality latency. Token cost is
zero in every code path; no provider was called or charged. The engine loses the cold-start time comparison
to cached direct for the measured first 24 requests. Do not multiply these numbers into customer ROI claims.

A production A/B study needs independently authorized buyer/seller traffic and identical eligible opportunities,
with actual request/billing records and agreed outcome definitions. No live LLM or customer study was fabricated.
