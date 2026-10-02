# Engine open-source release — 2026-10-03 KST

This is the initial extraction snapshot. The later unified console, durable
sync and venue execution extension is tracked in [UNIFIED_ENGINE.md](UNIFIED_ENGINE.md)
and its release verification report. Limits below describe the initial release.

## Delivered

The landing page opens a separate Economic Machine Engine: Overview, Connections, Agents & limits,
Activity and API usage. The public view joins one actual completed x402 Arbitrum Sepolia purchase. The
MIT-licensed standalone runtime provides owner-authenticated, loopback-only APIs and the same console
for user-owned account readers, durable metadata, native-unit balances, imported Spot orders, reviewed
programs, bounded capital, receipt replay and reported API usage.

The original chain-independent core was extracted with its ISA versions, capital locks, pause/revision
semantics, state-driven wakes, unknown-state outbox and bounded inference assessments. No dataset,
TRON product adapter, model weight or generic new signer was added to the Engine.

## Validation

All execution ran at `/srv/skew/economic-machine-commerce-20261002` on the owner-selected Canada host.
The Mac handled source edits, small reads, remote orchestration and browser review.

| Scope | Evidence |
| --- | --- |
| Extracted core, inference and specification | 47 passing tests; 195 pinned portable transition vectors, receipt identities preserved |
| New connections, local owner API and public Engine integration | 28 passing tests: credential boundaries, decimals, stale reads, disconnect races, request bounds, record corruption |
| Joined purchase / signature / delivery evidence | 20 passing targeted tests, including negative join and corruption cases |
| CLI service | A real isolated process; owner/origin gates, private DB, compile endpoint and clean termination |
| Live wallet reader | Real finalized-height Arbitrum Sepolia read: 0 ETH / 19.99 Circle test USDC at block 315012953 |
| Signing / submission side effects | None in release verification; zero new signatures and no transaction transmission |
| Source quality | Ruff on changed/extracted Python and Node syntax checks for changed JavaScript |

Durable records are in [submission artifacts](../artifacts/submission). Existing commerce/model/escrow
benchmarks were not rerun; their scope and dates remain documented separately. The chain read verifies
a public disposable wallet through an RPC; it is not an independent L1 finality proof.

## Scope and remaining implementation

Binance Spot supports account and open-order **reads**; live credentials were not supplied for a live
Binance test. Models/catalog and data-reader tests use fixtures; the existing Qwen study remains its own
historical evidence. Adapter-reported cost is not a provider invoice. Derivative positions, generic live
exchange dispatch, automatic connection scheduling, public multi-tenant administration and a new
browser-signed checkout are not implemented. No mainnet audit or hedge-fund production certification
is claimed. A current kernel evaluation is an intent, not a fill.

The software is meant to accumulate tested economic semantics and useful adapters. Physical and
nonblank line counts are recorded in `public-source-audit.json`; generated JSON vectors, logs, images,
dependencies and Git objects are excluded. Line counts do not measure correctness or production readiness.
