# Unified Engine verification — 2026-10-03 KST

All compute: owner-selected Canada host `148.113.153.116`, isolated repository
`/srv/skew/economic-machine-commerce-20261002`. Source edits and browser review
used the Mac control terminal. No new customer signature, order, payment or
chain transaction was sent.

## Evidence

- 89 distinct changed-code and connection checks: 34 new execution/transport,
  scheduling and isolation tests, and 55 existing connection regressions.
  The first integration run exposed an obsolete standalone page-mode assertion;
  the SELF_HOSTED mode marker was restored and that case was rerun successfully.
  Later changes used focused regressions; the unchanged 195-vector core and
  existing commerce/model benchmarks were not rerun.
- Ruff checks and Node syntax checks passed for changed backend and console code.
- Real Uvicorn/loopback HTTP: an enabled public-wallet read job ran, the process
  stopped and restarted, and its persisted job produced a newer observation.
  Both reads retained block/source evidence. Zero financial transmissions and
  no LLM path were used. See `artifacts/engine-release/runtime-sync-proof.json`.
- Actual, pinned HTTPS instrument reads succeeded for both Binance Spot and
  USD-M BTCUSDT. The futures catalogue needed a bounded 4 MB allowance on its
  fixed public GET endpoint; signed account/order responses retain the 500 KB
  bound. See `artifacts/engine-release/live-venue-read.json`.
- Public `/commerce/engine` redirects to `/commerce/console`. The unified page
  preserves wallet sign-in, API keys, payment limits and activity, and joins
  Overview, Connections, Agents & limits, Playground, Execution and API usage.
  Public recorded balances are historical and explicitly stale.

## Execution boundaries

The live adapter supports Binance Spot LIMIT orders and USD-M one-way reduce-only
LIMIT orders. Exact instrument rules, an immediate account re-read, immutable
turnover limits, exact-plan owner approval and a 60-second plan TTL gate dispatch.
Persistent reservations survive timeout and process restart. Query-based recovery
uses the original client order ID, bounded exponential backoff and venue Retry-After.
An ambiguous response never starts a second order or releases the hold.

The generic interface is an adapter port, not a claim that every exchange is
integrated. New leveraged positions, market orders, withdrawals, arbitrary
broker URLs and generic wallet signing are unavailable. Private exchange reads
and actual fills remain unverified without owner-provisioned credentials and a
separately authorized order. Commission totals remain explicitly unreconciled
until venue trade-history integration is added.

The deployed services keep `ENGINE_ALLOW_LIVE_TRADING` disabled. Commerce x402
limits remain separate from venue turnover. Old API keys gain no new permissions;
engine scopes are explicit and cannot approve or transmit. Hosted credentials
are references restricted to the stable owner's namespace, and private databases
have mode 0600 in a 0700 directory.

Source and selected Git blobs were scanned for credential patterns and private
file types on Canada. No findings were returned; this is not a universal secret
detector or a production security certification. Physical/nonblank line counts
exclude JSON vectors, logs, screenshots, dependencies and Git objects.
