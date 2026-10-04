# Skew Solution protocol and signer

The Arbitrum One-compatible emission contract works with a separate owner-local
miner and signer. Source availability does not establish deployment, funded VRF,
public token issuance or an independent security audit. Research contracts and
requester-funded mining use separate reward semantics.

## What a participant runs

A user runs the C++ search pipeline and the read-only `solution_operator.py` on their
own Linux host. Their CPU produces candidate bitsets; AI APIs are optional candidate
sources, never admission/reward evidence. Native workers have no network/signing authority.
`solution_operator` registers local work once per round/problem, reserves edge-visit
budgets, verifies scores and saves commitment salts privately before export.

The signer is a separate owner process using `solution_signer.py`, a private encrypted
JSON keystore and an interactive password prompt. It never sends a key to Skew, the
C++ worker or a public API. Python key objects are not a guaranteed memory-zeroization
mechanism; a hardware-wallet signer remains a separate integration.

1. Compile/review the pinned C++ binary on Linux. Existing remote builds record the binary
   SHA256 and sanitizer results. Frame ABI is x86_64 little-endian, not a portable wire ABI.
2. Create a private 0700 miner directory and 0600 operator configuration with two distinct
   RPC hosts, the deployed mining address, **observed deployed runtime** SHA256, miner
   address, `chain_id: 42161`, executable/pin and edge budgets. Source checkout includes
   the existing `scripts/build_solution_pipeline.py` for `/srv/skew` builders. An owner
   using another Linux build location can compile `native/src/solution_pipeline.cpp`
   with `g++ -std=c++20 -pthread -Wall -Wextra -Werror -O3` and verify the resulting pin.
3. Run `python scripts/solution_operator.py --config /private/operator.json run --watch`.
   Inspect the private `jobs` table in the miner journal; select a solved qualifying job.
4. `prepare --job JOB --action commit`, then `export --intent ID --output /private/commit.json`.
   Repeat for reveal only in its reveal window, and claim only after finalized winner readback.
5. On the owner signer host, create a separate private signer config with exactly:
   `rpc_a`, `rpc_b`, `chain_id`, `contract`, `code_sha256`, `owner`, `directory`,
   `per_tx_limit_wei`, `daily_limit_wei`. Runtime and owner/limits bind the signer database;
   changing them silently is rejected.
6. Supply a 0600 fee file with exact integer `nonce`, `gas`, `maxFeePerGas`,
   `maxPriorityFeePerGas`. Obtain nonce and current fees from your wallet/RPC and simulate
   the exact transaction. Arbitrum execution includes the L1-data gas component; the
   disposable EVM gas report does not estimate mainnet posting/VRF cost.
7. `python scripts/solution_signer.py --config /private/signer.json sign --intent /private/commit.json --fees /private/fees.json --keystore /private/wallet.json`.
   This signs locally and returns only an identifier, hash and reserved maximum cost.
8. Review that hash and fee envelope, then explicitly `broadcast --id ID --approve-tx-hash 0xHASH`.
   Save the returned hash into the miner with `observe --intent ID --tx-hash 0xHASH`.
   Reconcile both journals. Unknown transmission blocks further admission; do not resend.
9. For each later action repeat owner approval. The supplied CLI does not grant unattended
   signing or a server-wide key. Contract `claimMany` supports atomic batches of up to 16;
   this signer intentionally admits single claims with exact mint-receipt verification.

Signer allowlist: `commit`, `reveal`, `claim`, `finalize`, zero ETH value, pinned contract
and selected chain only. No approvals, token transfers, wallet creation, arbitrary router
calls, keeper requests or governance calls pass this boundary. The broadcaster persists
UNKNOWN before touching the network and uses a single request without redirects/retries.

Waiting for L1 finality would miss the 10-minute windows. Nonce progression therefore admits
an earlier transaction after two providers agree on its canonical receipt **and** it lies
behind the configured recent-block confirmation margin. Such receipts remain
`PENDING_FINALITY`; they do not release cost reservations or prove reward ownership.
At most 32 outstanding records are permitted. Missing/held/orphaned records stop progression.
Rewards still require finalized mint events, claimant/winner evidence and token balance readback.
Providers on different hostnames are not proof of independent operators or cryptographic truth.

## Contract and economics

`SkewSolutionMining` creates the immutable `SkewSolutionToken` with zero initial supply,
160,000-token cap, 10,000 rounds, 16 problems and one token per winning problem. Token name
`Skew Solution` and symbol `SKEW` are **candidate release parameters requiring owner review**.
Only the mining contract can mint. No premine, upgrade, discretionary reward writer or
administrative mint exists. A per-round emission bound holds regardless of wallet count.
Unqualified/unclaimed slots mint nothing. No customer revenue or token price is assumed;
mining may cost more than rewards and random Max-Cut work is not evidence of useful demand.

The full graph has 32 nodes and 496 positive integer weights. Seed/problem/edge hashing and
score exactly match the existing C++ verifier. Solvers do not prove global optimality.
Highest qualifying score wins, with earliest commitment ordinal breaking ties. Commitments
bind chain, deployment, round, problem, sender, bits and private salt. Complement symmetry
is removed by fixing bit 0 to zero. Copied reveals cannot change the recipient. Independent
identical answers still use the published ordinal rule; there is no identity-based Sybil claim.

Threshold updates apply to future rounds, use four finalized qualification counts and move
by at most 100 bps inside 5,200–6,000 bps. Solver dominance, concentration and deliberate
answer withholding remain economics/research risks; a contract bound does not prove fairness.

The governor cannot rewrite past scores/rewards. Two-step governor rotation invalidates every
old keeper. Revocable keepers alone may spend VRF subscription funds on new requests; interval
is at least 30 minutes. Initial admission is locked. Activation and each request require funded
LINK reserve and this contract's presence on the coordinator subscription consumer list.
Pause blocks new requests/commitments while preserving existing reveals/finalization/claims.
Governor/keeper liveness is an explicit operational dependency.

VRF configuration is drawn from [official Chainlink network documentation](https://docs.chain.link/vrf/v2-5/supported-networks).
Requests use three confirmations, a 250,000-gas callback, one word and LINK payment.
The immutable boundary uses the coordinator ABI directly rather than inheriting the official
migratable consumer base; review this deliberate deviation independently. Coordinator code
and subscription must be inspected before deployment. The documented
[VRF security model](https://docs.chain.link/vrf/v2-5/security) requires avoiding re-rolls.
This version never cancels a pending request, discards a late seed, or requests a replacement.
Duplicate callbacks cannot rewrite a started round. A malformed authenticated callback leaves
the round pending, emits a diagnostic and does not invent randomness. Availability failures
need coordinator/subscription incident handling. Search starts after seed disclosure, with
fixed commit/reveal windows; this is optimization competition, not a pre-draw betting scheme.

## Compilation and deployment prerequisites

`compile_solution_mainnet.py` pins Solidity 0.8.24 by binary SHA256, Shanghai,
optimizer 200/viaIR, zero warnings, EIP-170/EIP-3860 limits and source/artifact hashes.
`validate_solution_mainnet.py` runs contract, signer and chain-lifecycle tests. It retains log hashes and source
pins. `SkewVRFMock` is a test-only coordinator without a cryptographic randomness proof.

`solution_mainnet_preflight.py` performs only read methods: actual chain ID, common finalized
block, official coordinator/LINK code agreement and optional subscription balance/consumers.
It records unavailable reads separately. With public owner address, subscription ID and LINK
reserve, it generates the exact unsigned constructor, initcode hash and release parameters.
No ETH transfer, subscription funding, signature or deployment occurs in preflight.

Actual public mining remains closed until an owner-reviewed deployment, funded subscription,
consumer registration, activation, actual VRF request/fulfillment and independent participant
commit/reveal/claim have finalized and balances have been checked. Independent security review,
completed duration qualification, keeper incident procedure and off-host secret recovery also
remain production launch gates. Duration qualification must finish before it is reported as completed.
