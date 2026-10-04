# Deployment, VRF administration and security review packet

This work is not an independent audit or a completed deployment. No mainnet operating
wallet/profile was found in the inspected project configuration. Existing disposable
commerce signers are not assigned mainnet authority. Mainnet signing, deployment and
LINK transfers remain zero. Owner wallet and reviewer questions are pending.

## Implemented administration path

`solution_setup.py` creates exact unsigned administration reviews. The participant signer
still cannot create subscriptions, deploy, register consumers, fund LINK or activate mining.
Administration is deliberately a separate owner-wallet path.

- `create-subscription`: quotes the official coordinator's `createSubscription()` against
  two RPCs and records wallet nonce, balance and maximum gas envelope. The subscription ID
  must be obtained from the actual finalized creation receipt and coordinator readback;
  a nonce cannot predict that ID. This command never invents a subscription or sends a tx.
- `deploy`: with an existing verified owner subscription, builds and quotes the pinned
  compiled constructor. The contract begins locked with zero token supply.
- `register-consumer`: checks subscription owner and deployed governor, coordinator,
  subscription, key hash, LINK reserve, emission constants and reward-token invariants.
  Actual runtime bytes must match compiler templates outside immutable slots. An ABI
  imitation does not pass this check.
- `fund`: admits only LINK's `transferAndCall` to the official coordinator, exact LINK
  amount and ABI-encoded subscription ID. It enforces the configured LINK cap, uint96
  balance range and observed wallet LINK balance. No ETH funding alternative, token
  approval, arbitrary destination or market purchase is supplied.
- `activate`: requires observed consumer membership and sufficient subscription LINK
  balance, plus the currently locked, unactivated contract. Verify reads again afterward.

The [official subscription workflow](https://docs.chain.link/vrf/v2-5/subscription/create-manage)
explains createSubscription, consumer registration and ERC677 LINK funding. Addresses and
key hashes come from [official network configuration](https://docs.chain.link/vrf/v2-5/supported-networks).
Arbitrum estimates must include [L1-data posting cost](https://docs.chain.link/vrf/v2-5/arbitrum-cost-estimation).

Create a 0600 owner-side setup config containing `rpc_a`, `rpc_b`, `chain_id`, `owner`,
`maximum_gas_wei`. Later stages add the actual `subscription`, `mining`, `code_sha256`,
`minimum_link_reserve_juels`, and (only for funding) `fund_link_juels`/`maximum_link_juels`.
Never include private keys or passwords. For example, after configuration:

```
python scripts/solution_setup.py --config /private/setup.json create-subscription --output /private/create-review.json
```

Each later stage produces a new exclusive output file. It never overwrites an approved
review. Wrong chain, pending wallet transactions, nonce disagreement, excessive fee quote,
wrong subscription owner, wrong code/configuration or inadequate LINK blocks the relevant
stage. An insufficient ETH balance is explicitly reported, never treated as funding success.
These are preparation commands, not broadcast commands. Owner deployment/funding must be
approved using the real address, exact review, amounts and current fee envelope.

## Signer issue found and fixed

The previous owner signer rechecked predecessor receipts when preparing the next nonce,
but did not recheck them again immediately before transmission. A canonical pending
predecessor could become UNKNOWN/ORPHANED between signing and broadcast. The new code
performs that predecessor check inside the same durable transaction that reserves UNKNOWN
before its single network submission. It blocks transmission after uncertainty without
retrying or releasing gas reservations. A disposable-EVM regression covers both states.
This is a direct implementation review finding, not an external auditor's finding.

## Automated Solidity analysis

Slither 0.11.3 ran remotely with the pinned Solidity 0.8.24 compiler, viaIR, optimizer 200
and Shanghai. Raw JSON/logs are preserved under `artifacts/solution-release/security`.
The analyzer completed: 100 detectors, 18 findings (7 Medium, 11 Low), no High severity
finding reported by this tool. An exit status indicating findings is retained; it is
not changed into a clean security result. Absence of a tool finding is not assurance.

| Detector | Count | Initial assessment and remaining verification |
| --- | ---: | --- |
| incorrect-equality | 1 Medium | State==3 is the finalized state number, not a financial balance equality. Existing phase/claim tests exercise this; independent reviewer should confirm the state machine. |
| uninitialized-local | 4 Medium | Solidity defines these value-type locals as zero/false. Existing score/difficulty/subscription tests exercise those defaults. Do not infer analogous safety for C++ variables. |
| unused-return | 1 Medium | The LINK-only admission check deliberately reads LINK balance and consumer list, not native balance/request count/owner. New admin preparation separately checks subscription ownership; the LINK payment flag is fixed false. |
| reentrancy-no-eth | 1 Medium | Request uses the immutable coordinator, a request lock and pending-round phase gates. Request-ID storage must follow the coordinator response. Cross-entry behavior and read-only partial-state observers still require the independent reviewer; this finding is not unconditionally dismissed. |
| calls-loop | 1 Low | Batch size is capped at 16; token is created by the constructor and has no recipient callbacks. Existing atomic-rollback/claim tests cover the bounded loop. |
| reentrancy-benign / reentrancy-events | 3 Low | Same coordinator boundary or immutable reward-token call; review alongside the Medium reentrancy item rather than waive the detector globally. |
| timestamp | 7 Low | Time windows deliberately use chain timestamps. Sequencing and inclusion near boundaries remain liveness/fairness assumptions; a 60-second client margin is not a guarantee. |

This assessment is authored by the implementing assistant and supported by an independent
analysis tool. It is **not** a review by an independent engineer, an external audit,
a formal proof, or a production launch approval. The requested separate reviewer has not
been assigned yet. All raw findings remain available for that reviewer.

Review scope: mining/token contracts, immutable VRF boundary, emission cap, commit/reveal
binding, pause/role transitions, late fulfillment, batch claims, private miner/outbox,
nonce progression, unknown submissions and administration calldata. Also review
subscription-owner/keeper availability, solver concentration, difficulty manipulation,
key recovery and actual Arbitrum finality assumptions. Random graph search is not evidence
of useful commercial demand or guaranteed mining profitability.

## Current release gate

Final remote validation passed 23 changed-code/connection cases with zero failures,
errors or skips, and Ruff passed. All eight source hashes matched the local checkout.
An initial invocation started before synchronization completed and could not import the
new setup module; that failed log is retained as `focused-tests-initial.log`. The final
`focused-tests.log` and `validation.json` identify the successfully verified sources.

Automated and focused test evidence supports the changed implementation only. Mainnet
operating wallet, actual subscription, signed deployment/funding, an independent reviewer,
completed sustained qualification and a finalized public participant lifecycle remain
unverified. The repository's prior PR7 evidence applies to its original source hashes;
new signer/admin evidence is stored separately, without relabeling historical results.
