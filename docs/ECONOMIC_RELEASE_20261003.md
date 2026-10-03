# Native economic decisions and public DataPass deployment

Implementation commit: `ae7fa42bc53ade01996a82d4e51830372a53617c`.

The financial engine now computes typed state, bounded financial decisions and
transaction projections in C++20. It connects to the existing console workspace
and named-agent journal. The following are software calculations under supplied
assumptions, not live investment results or a claim of general intelligence.

## Original economic-computer responsibilities

| Responsibility | Executable implementation |
| --- | --- |
| State | Typed units, quote/base assets, source generations, sequence gaps, atomic batches, freshness and dependency frames |
| Opcode | 39 typed math, control and economic instructions; forward-only branches and definite-assignment validation |
| Invariant | Amount overflow, account/network/currency identity, cash floor, cost, leverage, collateral, concentration and scenario limits |
| Transition | Proposal/simulation/authorization/inclusion/finality/post-state/settlement mirror; ambiguous outcomes retain reservations |
| Receipt | Native diagnostic result plus the existing workspace's SHA-256 journal; native identifiers cannot substitute for signed chain evidence |
| Economic primitives | Oracle aggregation, lending/yield, linear derivatives/funding, recovery/repayment/rebalance, AMM and depth routing, allocation search, VaR/CVaR and factor risk |
| Execution planning | Dependency graph, conservative credit floors, intermediate funding/health and common terminal targets |
| Continuous observation | Fixed C++ event runtime, dependent-program scheduling, expiry interrupts, coalescing and queue backpressure; live feed wiring remains pending |

The public console's financial Playground actually calls the pinned C++ shared
library. Synthetic collateral of 100 changes REDUCE to HOLD when changed to 200.
The same program is tested through the owner's workspace and named-agent task.
Wide integers retain their exact value in browser receipts through an explicit
`{"integer":"canonical decimal"}` encoding. Editing an input hides its old decision.

## Reproducible evidence

- `economics-build.json`: 287 checks normally and the same 287 with ASan/UBSan.
- `economics-integration-tests.log`: 15 actual C++/workspace/API/scope integration tests.
- `economics-portal-final.log`: focused final check after changing portal library loading.
- `economics-live-check.json`: public API REDUCE/HOLD readbacks, exact receipt hashes,
  protected-route 401 and two finalized RPC observations of deployed contract code.
- `economics-independent-verification.json`: a fresh public GitHub checkout on the
  authorized Canada host rebuilt the same library bytes and reran the 287/287 and
  15 integration checks. Its 219-file release manifest also matched. Rebuilding
  changes the build report's local path; that generated report was restored before
  comparing the public manifest. No implementation source was altered.

An initial readback immediately after service restart did not confirm the protected
route. Its status was not retained, so it is not labelled an authorization bypass.
The later independent request and final live evidence both returned 401.

The C++ library SHA-256 is
`0cf9b82bb5c362ae2b1b6c402a2038a8f4706f15f23cfd27db56cb346d271035`.

## Public settlement boundary

DataPass contract, Arbitrum Sepolia:
`0x2C9619Cd327418571963A3334EA674FA1F4Fb234`.

Deployment transaction:
`0x6edc6f928c7e48aabdf8b1140eaa024ddd1e02084ea8e3278fdc597808160bc8`.

The deployment proof records two RPCs, finalized block 315190340, receipt status 1,
byte-for-byte runtime matching, owner/publisher/token configuration and test-gas cost
0.0002253959043 ETH. No DataPass purchase, new license mint or customer-funds transaction
is asserted. The C++ engine remains off-chain.

## Source count

At the implementation commit, excluding blank lines, data, evidence, dependencies,
docs and landing copy: implementation 20,200; tests 6,734; operational tools 3,290.
The combined authored code/test/tool count is 30,224. Native C++ implementation alone
is 6,310. The **30,000 implementation-only objective is not met**; these categories
must not be silently combined to claim that it is. The line audit retains per-file
counts and the unchanged implementation-only denominator.

Cross-margin and inverse derivatives, options, calibrated market prediction,
general protocol lowering and full live numeric-feed deployment remain unimplemented.
Neither line count nor a testnet contract deployment proves those capabilities.
