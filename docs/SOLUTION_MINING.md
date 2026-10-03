# SKEW Solution Mining — research mode

**A verified solution earns one capped reward. API spend, capital and wallet count earn none.**

This adds `MAXCUT_V1_RESEARCH` beside requester-funded Machine Mining in the same Economic Machine console. It is a narrow research protocol, not an assertion of economically useful work, token value or miner profit. A C++ worker searches integer weighted Max-Cut; Solidity replays the score. No AI judge determines the winner.

## Fixed protocol

- Each VRF-backed round has 16 problems, each a complete graph of 32 nodes and 496 edges. Weights are integers 1–1024. These sizes are V1 bounds, not proven ideal mining economics.
- The graph is deterministically expanded from an immutable seed with `keccak256(abi.encode(keccak256("SKEW_MAXCUT_V1"), seed, problem, edge_index))`. Edge order is increasing `(u,v)` with `u < v`.
- A solution is a 32-bit partition. Bit 0 must be zero; the complementary partition normalizes to the same canonical solution.
- The score is the sum of weights crossing the partition. A result qualifies when `score*10000 >= totalWeight*roundThreshold`.
- A valid winner earns 1 SKEWSIM per problem, at most 16 per round. Unqualified problems mint nothing. No per-wallet or per-submission multiplier exists.
- The research token has a hard cap of 160,000 SKEWSIM over at most 10,000 rounds. No premine or administrator mint exists. Token parameters are research fixtures, not a launched monetary policy.
- A winner can claim only once, after finalization. Actual `mint` authorization belongs only to the deployed research protocol. The token has no demonstrated price, redemption or revenue.

The current contract/token have **not been deployed publicly**. Tests execute real compiled bytecode in a local EVM on the Canadian host; any tokens minted there are simulation state. The console can only calculate/download candidates and cannot request paid randomness, sign, issue tokens or broadcast.

## Commit/reveal and ordering

`VRF request → immutable seed → 10-minute commit window → 10-minute reveal window → finalize → claim`

Commitment binds `contract`, `chainId`, `round`, `problem`, `miner/reward recipient`, `uint32 bits` and `bytes32 salt` with `abi.encode` and Keccak. Commit closes before reveal opens. One address commits once per problem. Equal best scores use earlier commitment ordinal; an independently discovered duplicate can still lose this priority contest. Reveal order does not choose the winner.

Different wallets cannot replay a copied commitment or change its reward recipient. A repeated canonical answer receives only one problem reward. This is not proof of one-human identity or decentralized mining: one strong solver can win every problem.

## Randomness and failure

The protocol uses the **Chainlink VRF v2.5 request/callback ABI**. Only the immutable configured coordinator may deliver. That coordinator verifies the cryptographic VRF proof; our consumer does not duplicate the proof verifier. A valid provider deployment, funded subscription and registered consumer must be independently checked before using this outside tests. No blockhash, timestamp or administrator-chosen seed fallback is implemented.

Pending rounds cannot be rerolled. If a callback is missing after one hour, malformed, late or repeats a used seed, the round aborts and issues nothing. Late/repeated callbacks cannot restart it. Only one round is active; requests are limited by a 30-minute minimum cadence and the lifetime round cap. The coordinator callback only stores seed and deadlines; it does not compute 16 graphs.

`SolutionVRFMock.sol` is explicitly a **test coordinator without proof verification**. A passing mock callback is not evidence of public VRF randomness. No live subscription is configured or charged by this implementation.

Sources checked for interoperability: [official VRF request interface](https://github.com/smartcontractkit/chainlink-brownie-contracts/blob/main/contracts/src/v0.8/vrf/dev/interfaces/IVRFCoordinatorV2Plus.sol), [official client encoding](https://github.com/smartcontractkit/chainlink-brownie-contracts/blob/main/contracts/src/v0.8/vrf/dev/libraries/VRFV2PlusClient.sol), [current supported networks](https://docs.chain.link/vrf/v2-5/supported-networks). The archived ABI examples are compatibility references; current provider addresses/subscription settings require live validation.

## Bounded difficulty controller

The initial threshold is 55%, the floor 52%, the ceiling 80%. Only finalized rounds enter the four-round ring buffer of qualified-problem counts. At least 75% qualified raises the next threshold by 100bp; at most 25% lowers it by 100bp. The current round's problem, threshold, windows and reward never change. Timed-out randomness is excluded from the controller.

The controller uses result qualification, not wallets, deposits, self-reported GPU time or API usage. Withholding can still drive qualification down and reduce future difficulty; the eight-round withholding test reaches the floor without minting. Bounds limit the attack's effect, but do not eliminate the strategic incentive.

## Confirmed admission weakness: production gate remains closed

Each problem admits at most 64 commitments to bound storage. The attack test confirms that 64 wallets can fill those slots and exclude the next miner. It also confirms that doing so does not increase issuance. Gas costs alone are not represented as complete Sybil resistance. This release has no admission bond, identity proof or competitive-slot replacement scheme.

Therefore **real issuance remains a closed gate** until an admission mechanism is designed, adversarially measured and reviewed. No statement of production-ready mining, profit or decentralization follows from the current tests. The UI does not expose an issuance button.

## Native worker and hardware boundary

The hot calculation path is C++20: fixed arrays, contiguous edge data, a fixed adjacency matrix, integer score/delta arithmetic and a bounded result queue. Search and batched candidate staging allocate no heap memory or language tokens. The queue is worker-local, not an inter-thread lock-free queue. No AF_XDP, SmartNIC, FPGA, NUMA tuning, CPU affinity, GPU kernel or SIMD speedup is claimed.

Workers support random sampling, greedy local search and integer annealing. A separate exact enumerator is restricted to at most 20 nodes and used for small reference problems. Heuristics never claim global optimality. All workers use a bounded edge-visit budget. Network waits, secret persistence, API calls and signing remain outside the native call.

Role boundaries:

| Role | Implemented behavior | Remaining boundary |
| --- | --- | --- |
| Chain/round source | Contract-owned round state, request IDs, immutable seed/window and timeout | No live subscription/automatic RPC watcher configured |
| Work assignment | Problem index, selected CPU algorithm, bounded edge visits | No GPU lease or paid-provider daily-budget loop |
| Search worker | Native CPU random/greedy/anneal, exact-small reference | GPU/AI results unmeasured |
| Local verifier | Exact score replay, canonical bits and graph receipt | Frozen research input, not external economic utility |
| Submission manager | Sender-bound offline commit/reveal, exclusive 0600 salt file, restart-readable reveal | No broadcast; ambiguous chain outcomes must be reconciled before retry |
| Signer | Unsigned narrowly typed transaction drafts only | Owner approval, deployment/code/round checks and wallet signing required |

## CLI / arbitrary-agent integration

Build using `python scripts/build_solution.py` on Linux under `/srv/skew`. Export the immutable `library_path` and `library_sha256` from `artifacts/solution/native-build.json` as `ENGINE_SOLUTION_LIBRARY` and `ENGINE_SOLUTION_SHA256`. Use `PYTHONPATH=src`.

```sh
python scripts/solution_cli.py search --seed 12345 --problem 0 --algorithm integer_anneal --budget 1000000
python scripts/solution_cli.py verify --seed 12345 --problem 0 --candidate my-agent-answer.json
python scripts/solution_cli.py seal --seed 12345 --problem 0 --candidate my-agent-answer.json --contract DEPLOYED_ADDRESS --round 1 --miner YOUR_ADDRESS --secret-file solution-secret.json
python scripts/solution_cli.py reveal --secret-file solution-secret.json
```

The candidate file contains an exact decimal `bits` string. Any agent can generate it; our verifier ignores claims about API consumption or intelligence. Sealing independently recomputes the score. Provider keys are not uploaded. Secrets are created exclusively with mode 0600 and cannot be overwritten. Commit output omits salt; reveal calldata exposes it and must stay private until the reveal phase.

Offline drafts do not authenticate an on-chain round. Before signing, verify deployed code/coordinator, actual seed/threshold/window and existing commitment. Do not auto-retry unknown submissions. This CLI does not read or use wallet private keys.

## Measured research evidence

`artifacts/solution` retains the initial canonical-encoding failure and the corrected passing tests, compiler/source hashes, sanitizer result, real isolated/public UI checks, and benchmarking. It tests 16-problem native/Solidity score agreement, capped local issuance, replay/recipient binding, canonical duplicates, no-result issuance, future-only difficulty, spoofed/late randomness, withheld answers and slot saturation.

Benchmarks compare CPU random/greedy/anneal over 16 fixed problems with (a) the same edge-visit budget and (b) separate 20ms native-call wall-clock budgets. These are **not equal-dollar-cost measurements**: hardware rental, electricity, GPU billing and AI provider invoices are unavailable. Eight small problems also use exact solutions to measure gaps. Native score latency and actual local-EVM score gas are separately reported; source generation, VRF proof, network/chain latency and settlement cost are excluded from the native figure.

GPU comparison is `NOT_RUN_NO_AUTHORIZED_GPU_LEASE`; AI comparison is `NOT_RUN_NO_APPROVED_PAID_PROVIDER_BUDGET`. No GPU/AI superiority is inferred. A random graph is not automatically useful economic output. The next decision is whether measured difficulty, verification cost and admission security justify this task family; otherwise change task family before public token issuance.

Solidity was chosen for the bounded verifier. The current [Stylus pause notice](https://docs.arbitrum.io/notices/stylus-activation-pause-notice) specifically covers new/expired activations on **Arbitrum One and Nova**, and says Solidity/EVM execution is unaffected. It is not a blanket claim that Sepolia Stylus is unavailable.
