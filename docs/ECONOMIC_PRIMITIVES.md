# C++ Economic Machine primitive library

The runtime follows OBSERVE → TRANSITION → VERIFY with
typed economic instructions, bounded capital, external settlement and reproducible receipts.
The native economic library implements financial calculations and decision functions inside
that boundary.

## Executable domains

| Operation | Actual computation | Limit |
| --- | --- | --- |
| ORACLE_PRICE | Median, confidence envelope, freshness, source uniqueness, dispersion | Adapter verification is asserted at the input boundary; no native signature verification |
| LENDING_RATES | Kinked utilization model, reserve-adjusted supplier APR | This declared interest model; not a live protocol rate |
| FORWARD_YIELD | Base interest, retained incentives, exit haircut, fees | Simple interest under assumptions; no promise of realized APY |
| LENDING_HEALTH | Conservative collateral/debt values, capacities, health | No cross-asset valuation without supplied prices |
| DERIVATIVE_RISK | Linear P&L, equity, margin, leverage | Isolated linear model; not inverse or cross-margin venue accounting |
| FUNDING_CASHFLOW | Signed payable/receivable funding with conservative rounding | One supplied settlement rate |
| DERIVATIVE_RECOVERY | Enumerated close sizes/routes, simulated post-state, least-cost feasible reduction | Bounded lot search; incomplete search returns no optimum |
| REPAYMENT_DECISION | Smallest repayment meeting health and cash reserve | Debt-value projection; token conversion remains the external compiler's task |
| REBALANCE_DECISION | Net improvement after costs, uncertainty buffer and cooldown | Expected returns supplied by the caller |
| AMM_SWAP | Exact input/output constant-product simulation | Non-taxed, non-rebasing tokens |
| AMM_ROUTE | Up to four coherent, non-reused pools | No claim of a universally optimal route |
| ALLOCATION_SEARCH | Finite-grid enumeration, stress/liquidity/concentration/cost constraints, three alternatives | Only the explicitly declared grid is complete |
| VENUE_ROUTE | Depth and all costs across eligible venues | FOK/IOC/LIMIT simulation, no transmission |
| SCENARIO_TAIL_RISK | Weighted discrete VaR and exact tail-mass expected shortfall | Caller probabilities, not calibrated forecasts |
| RETURN_STATISTICS | Simple returns, variance, deviation, drawdown, time-weighted price | Fixed cadence required; no hidden annualization |
| FACTOR_RISK | Separate gross/net factor exposures and violated limits | Caller factor loadings |
| STATE_TRANSITION | Observation, proposal, simulation, approval, transmission, ambiguity, finality, post-state and settlement mirror | External verifier assertions; no native signer |
| STATE_FRAME | Typed canonical facts, source sequences, account/network/asset identities, atomic batches and dependency TTL | Input adapter identity is asserted, not authenticated by this calculation |
| ECONOMIC_PROGRAM | 39 economic/math/control opcodes, typed registers, definite assignment and forward branching | Action candidates only; SETTLE requests a verifier |
| EXECUTION_GRAPH | Dependency order, intermediate funding, quote floors, fees, cash/debt/health and terminal allocation targets | Single-account solvent projection; no protocol calldata or transmission |

The same header library also provides tiered margin, isolated liquidation thresholds,
funding, LP redemption, split-route candidate comparison, oracle caches and interest-first repayment.
Every exported operation checks the fixed ABI's input/output sizes. JSON parsing and journal I/O
remain outside the C++ arithmetic loop. No LLM, HTTP, heap-backed state store or signer runs inside it.

`EconomicRuntime` in `runtime.hpp` registers account/network-scoped programs, marks only dependent
programs dirty, coalesces events under a minimum interval and wakes on candidate/state expiry.
Its fixed SPSC queue retains pending results and applies backpressure to ingestion rather than
overwriting financial state. Idle monitoring neither re-emits missing-state exceptions nor invokes
a model. Feed adapters supply observations through the typed input boundary; the library does not
open market connections itself. The calculation API uses the same console service.

Execution projections treat `available` as unencumbered free tokens and `reserved` as supplied
collateral in the declared lending model. These fields do not release authority-ledger budget holds.
Borrow adds liability and proceeds; repayment removes both. Candidate graph comparisons require
the same initial balances, risk envelope and terminal targets, so a cheaper graph cannot silently
achieve a worse allocation. Existing adapter-specific order/calldata compilation remains separate.

## One console and one authority journal

`GET /api/engine/economics` exposes exact schemas and integer scales.
`POST /api/engine/economics/evaluate` evaluates an operation and appends its result to the
existing workspace's SHA-256 journal. Named agents can invoke `ECONOMIC_DECISION` only when
their existing shared policy permits it and the connection belongs to that workspace.
The normal agent run ID, request idempotency and run history apply. These tasks hold zero
venue turnover and cannot produce or approve an order automatically.

The unauthenticated portal's bounded example is labelled synthetic. Its timestamps and
positions are assumptions, not the current customer's account. Signed-in console operations
use the owner's actual workspace journal. The public example has no private account access.

Micro-units use scale 1,000,000; bps use 10,000; envelope time uses nanoseconds.
All scalar JSON values must be exact integers. Floating point, booleans used as amounts,
numeric strings, unknown fields and out-of-range values fail at the Python boundary.
The explicit `{"integer":"9223372036854775807"}` encoding preserves values beyond
JavaScript's safe-integer range; outputs beyond that range use the same typed literal.
Plain numeric strings, decimal fractions and noncanonical literals remain rejected.
The native library path is immutable and SHA-256 pinned. The existing native program
library remains separately pinned; no loaded shared-object file is overwritten.

## Verification and supported models

`scripts/build_economics.py` builds on Linux and runs the
conformance suite normally and under AddressSanitizer/UndefinedBehaviorSanitizer.
`tests/test_native_economics.py` exercises the real shared library through the same
workspace, HTTP routes and named-agent ledger. Funding rounding is differentially
checked against Python Decimal; one-condition counterfactuals change recovery/allocations.

An `UNKNOWN` external result keeps the hold. Pausing prevents fresh authorization.
Escalation preserves a pending state's recoverability. Settled capital charges the actual
observed amount/cost once. Inclusion, finality and post-state verification are separate phases.
The 64-bit native diagnostic fingerprint is explicitly not a cryptographic attestation;
the authority journal and external chain/signature verifiers retain that responsibility.

Still outside the declared financial models: cross-margin venue liquidation, inverse
contracts, options pricing/assignment, continuous-space global optimization, calibrated
market-regime prediction, general financial opcode lowering into every protocol adapter and deployment
of the off-chain C++ runtime inside a blockchain VM. A large source tree does not prove those.
