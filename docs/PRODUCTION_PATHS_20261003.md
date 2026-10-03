# Paid services, observed decisions and recovery — 2026-10-03

The Canadian deployment now connects compute purchasing and observed native
decisions to the existing owner workspace, approval boundary and durable ledger.
This change does not establish a completed customer payment or exchange fill.
Mac work was restricted to source editing and remote orchestration; all tests,
browser runs, workload measurements and backup rehearsals ran on Canada.

## Acceptance matrix

| Requested path | Implemented and tested | Actual external evidence | Remaining gate |
| --- | --- | --- | --- |
| 10-USDC subscription → confirmation → access → data | Existing hosted merchant, exact x402 payment, finalized-only access, owner isolation, delivery and expiry regression | Configured Arbitrum One recipient and previous unsigned 10-USDC challenge; current subscription UI rendered | Funded customer wallet signature; no paid subscription this turn |
| Compute purchase → execution → output | Admitted Gate402 inference, frozen request/price, shared mandate reservation, signature submission, independent finality, owner-scoped output and recovery | Actual provider 402 quote at 1,000 USDC atoms; public console quoted 0.001 USDC | Customer-approved payment signature; no actual paid inference this turn |
| Market → C++ → approval → execution → account | Four pinned spot/futures mainnet/testnet sources, deterministic statistics, source-bound expiring candidate, existing owner plan approval and independently read account | All four public feeds returned 30 closed BTC candles and actual compiled C++ calculation; console source → native HTTP path passed | Private exchange account; live transmission disabled; no actual order |
| Restart/outage operation | Restart-safe jobs, no resend on ambiguous payment/order, retained capital, account readback with observed/unverified status | Actual runtime restarted and became healthy; failure/restart cases passed with controlled external adapters | Real authenticated venue observations/fills and fee reconciliation |
| Backup/operations/performance | AES-256-GCM snapshot, integrity/foreign-key/journal checks, fresh-directory restore; pinned SSH ciphertext export/retrieval/restore; bounded concurrency measurements | Eight real runtime DBs plus seven private files encrypted/restored on Canada; browser desktop/mobile checks | A distinct approved backup host and separate external key custody; same-host restore is not off-host acceptance |

Subscription recipient: `0xD432a628a9860A8d0Be98c1782B2a0cD136Da00e`.
Arbitrum One Circle USDC: `0xaf88d065e77c8cc2239327c5edb3a432268e5831`.
Atlas Monthly is 10 USDC for 30 days; renewal requires another approval.
Provider prices expire and must be reviewed again before a customer signs.

## Compute execution contract

`POST /api/commerce/compute/jobs` requires `resource_id`, `request`, `max_total`
and `idempotency_key`. A request has a bounded allowed model, messages and token
limit. Price discovery grants no spending authority. The existing checkout
creates/reserves a payment under the owner's shared mandate, produces an exact
authorization, accepts an externally signed authorization, and reconciles it.
`GET /api/commerce/compute/jobs/{checkout_id}/result` requires owning read access
and finalized settlement plus delivered output. An agent may prepare a request
under its mandate; it cannot mint authority or generate a customer's signature.

The provider's documented text/camelCase usage response is normalized into a
bounded output envelope. The receipt binds request and response hashes; provider
usage/model labels are not proof of model truth, GPU ownership or a refund.
An interrupted request retains its hold. A paid request with missing output
remains `PAID_DELIVERY_MISSING`; recovery does not blindly resend a signature.
Inference API purchasing is not GPU-container leasing or physical capacity proof.

The configured resource allows a maximum 120-second owner authorization even
when the provider advertises a longer timeout. No provider timeout extends an
owner mandate. `deploy/compute.conf` uses systemd `LoadCredential` and the
operator-controlled private resource/compute files. The API cannot read the
subscription receiver keystore or passphrase.

## Observed decision and venue boundaries

Market connectors admit fixed HTTPS URLs and closed one-minute candles only.
They reject malformed prices, gaps, overflow and stale/future windows. HTTPS
observations are not signed price-oracle attestations or tick/order-book streams.
Spot, USD-M, testnet spot and testnet USD-M have separate source/account pairs;
a watch cannot authorize a different venue or network's policy.

Each decision pins source hash/version, immutable watch policy, native library
hash and expiry. The actual native ABI calculates return statistics and a bounded
drawdown rule generates `HOLD` or an owner-reviewable `PLAN`. It does not claim
LLM-level judgment or optimal profitability. Source changes or expiry block
approval/dispatch, including interruption before the plan-link commit.

Terminal orders query the exact venue order again and independently sync the
account. An unavailable account read preserves the terminal fill and records
`UNVERIFIED`; it never causes a second submission. Sequential account observations
are not causal fill attribution or commission reconciliation. First supported
execution adapters are Spot LIMIT and one-way USD-M reduce-only LIMIT, including
their fixed testnet endpoints. Hedge mode, opening derivative exposure and all
other venues are not implemented. Public transmission remains disabled.

For authenticated testing, use the owner's server-side environment namespace
returned by `/api/engine/profiles`; do not paste keys into chat or the console.
An isolated owner deployment, concrete order and per-order approval are required
before opening transmission. Exchange trading does not pay a commerce toll.

## Backup and evidence custody

Install the optional `backup` dependency on the trusted remote environment.
`sudo env PYTHONPATH=src .venv/bin/python scripts/backup_runtime.py` creates a
root-only encrypted snapshot and verifies a fresh restore. Each SQLite DB is
consistent independently; this is not a cross-database atomic snapshot. The
encrypted restore manifest maps DB basenames to their original directory class.
Restores never overwrite a live directory; swapping into a stopped service and
restoring service configuration remain owner-operated steps.

`--off-host-config /private/destination.json` accepts owner-private host, user,
directory, SSH identity and saved-host-key references. It requires a preexisting
0700 destination, different machine IDs, strict host-key checking, remote SHA256,
retrieval and successful decryption/restore of retrieved ciphertext. It transfers
no encryption key. Distinct host IDs are an operational check, not a cryptographic
proof of geographical independence. The owner must preserve the encryption key
separately off-host; losing this server and its only key defeats recovery.

No external backup destination was available this turn. The public evidence is
metadata and hashes only. Private archives, keystores, passphrases, credentials,
real runtime DBs and encryption keys remain outside this repository.

## Reproducible checks

`artifacts/production-paths/journal-boundary-regression.log` records 95 passing
connection, execution, agent-control and new-path tests. The separate
`payment-backup-regression.log` records 30 passing payment/merchant/backup tests;
five backup tests overlap the first suite. A separate eight-owner concurrency
test passed, including actual C++ HTTP handler calls and cross-owner credential
namespace rejection. This totals 121 distinct test cases across those suites.
Failed initial attempts remain in
separate logs. Static Python and changed JavaScript syntax checks passed.

`browser-live-paths.json` records six desktop/mobile checks, actual compute quote,
observed BTC window and actual native calculation with zero paid requests/orders.
`public-live-probes.json` preserves four real market reads and post-restart
production health. `backup-rehearsal.json` reports actual encrypted restore and
explicitly marks off-host verification false. `soak.json` and its preserved
before-optimization counterpart report the bounded HTTP/native/journal workload.

Journal appends fingerprint every stored historical field before reusing a prior
successful full hash-chain verification. Modification, restart or rollback
invalidates that cache. Tests mutate older JSON, hashes and metadata without
changing the tail. This avoids repeated JSON interpretation but still scans all
journal bytes; it is not constant-time append, external anchoring or administrator
tamper resistance. A fully rewritten internally consistent log is not detected.

The load run is one isolated workspace with eight persistent clients for ten
minutes, not eight independent customers, multi-process scale, a 24-hour soak or
chain/provider latency. It uses the already verified C++ library without changing
or rebuilding native source. No provider charge, LLM call or order is generated.
Host cost is not measured. Full paid-path latency/cost still requires actual
authorized payments and authenticated fills.

| Same bounded workload | Before journal optimization | After journal optimization |
| --- | --- | --- |
| Duration | 601.59 seconds | 600.61 seconds |
| Accepted requests | 3,424 | 4,800 |
| Failed requests | 0 | 0 |
| Median HTTP/native/journal latency | 546.6 ms | 133.4 ms |
| p95 latency | 1,699.3 ms | 490.4 ms |
| p99 latency | 2,126.6 ms | 671.9 ms |
| Deterministic result variants | 1 | 1 |
| Final journal verification | Passed | Passed |

This is a single before/after workload observation on a shared host, not an
isolated native microbenchmark or general throughput guarantee. Maximum offered
load is eight calls per second; no claim beyond the measured envelope is made.
The engine implementation audit counts 24,042 physical / 22,552 nonblank lines;
tests count 8,909 physical / 8,011 nonblank lines separately. These counts exclude
data, logs, dependencies and generated evidence. Nonblank includes docstrings;
tests are not added to implementation to claim 30,000 engine lines.
