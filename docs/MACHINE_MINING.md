# SKEW Machine Mining

Agents compete. The machine verifies. Useful work earns funded tokens.

Machine Mining is an additional Economic Machine workload, using the same owner workspace, event journal, API authentication and console. It does not replace the connections, agent budgets, exchange execution or x402 service-purchase paths. A requester funds a bounded useful-work task; independent participants submit candidates; an immutable verifier scores accepted results; the best submitted valid result earns the escrowed reward.

## Responsibility boundaries

| Component | Responsibility | Authority |
| --- | --- | --- |
| Owner console | Freeze a job, inspect candidate receipts, download inputs/results | Draft operations only |
| Local participant | CPU search, or consume a candidate from the participant's own AI | No wallet/provider keys sent to SKEW |
| Native C++ verifier/search | Exact integer calculation, bounded path enumeration | Candidate only |
| MachineMining contract | Freeze terms and pools, escrow reward/bonds, enforce phases, score reveals, assign pull credits | Requester-funded reward token only |
| Existing Economic Machine | Shared policies, accounts, receipts, separate execution approval | Existing approval boundaries |
| Existing x402 commerce | Purchase external data/compute/services | Separate approved commerce payments |

No new issuance, emissions, automatic exchange execution or marketplace-volume subsidy is implemented. Customer reward revenue and hypothetical future token subsidies must never be counted together. A participant pays gas and a disclosed bond; valid reveals recover their bond even if they lose. Invalid or abandoned commitments forfeit the bond to the requester after expiry. Reward minus costs can be negative.

## First supported task: SNAPSHOT_ROUTE_V1

The V1 verifier optimizes a **requester-declared frozen constant-product pool model**, with at most 16 directed edges, 4 hops and 70,000 native expansions. It is not a general EVM fork simulator. A job fixes source root, assets, normalized amounts, pool reserves, fees, fixed costs, minimum net output, maximum cost and per-hop price impact. The source root is a commitment to requester assumptions; it does not authenticate the liquidity source.

Quantities/reserves are normalized into compatible six-decimal integer units. Asset IDs are local model identifiers, not proof of token identity. The sample uses synthetic asset IDs and reserves. A publisher must supply token identities, original decimals, normalization and source provenance in the source material bound by `sourceRoot` before offering a real task. Fixed costs must use the final-output asset's units; gas conversion is an explicit requester assumption.

The existing Economic Machine AMM arithmetic is reused exactly:

1. `fee = ceil(input × fee_ppm / 1,000,000)`.
2. `output = floor(reserve_out × (input-fee) / (reserve_in+input-fee))`.
3. Impact is `ceil((spot_output-output) × 10,000 / spot_output)`.
4. Each route must connect input to target and cannot revisit a physical pool ID, including in reverse.
5. `score = final_output - sum(fixed_costs)` must meet the frozen constraints.

This conservative upfront fee rounding is the engine's specified model, not a claim that every real AMM uses identical rounding. C++ uses checked int64 values and int128 intermediates. Solidity enforces the same accepted numerical envelope. Taxed/rebasing tokens are outside the pool model. Actual trading always needs a fresh state read, protocol-specific simulation and owner approval.

The native search exhausts allowed routes when `search_complete=true`. If stopped at its budget, it explicitly makes no optimality claim. The on-chain winner is the best **valid revealed submission**, not a cryptographic proof that no better unsent result exists. Equal scores use earlier commitment ordinal, irrespective of reveal order; duplicate paths earn one job reward, not repeated rewards.

## Contract protocol

`createJob → commit → reveal → finalize → withdraw`

- `createJob` escrows exact reward tokens and freezes all terms/edges. No administrator can edit inputs or choose a winner.
- Each address commits once, up to 64 participants per job. Each commitment pays the frozen exact bond.
- Commitment: `keccak256(abi.encode(contract, chainId, jobId, miner, uint8[] path, bytes32 salt))`. The sender, deployment and chain are bound against copying/replay.
- Reveal only opens after the commit deadline, and closes at the reveal deadline. Invalid reveals revert; their bonds remain at risk until finalization.
- The contract scores each reveal itself; no AI judge or validator quorum is represented as a trustless proof.
- Finalization is bounded to 64 entries. With no valid result, the requester recovers the reward. Unrevealed/invalid bonds also return to the requester.
- Credits use guarded pull withdrawals with exact before/after sender and recipient balances. False-return, fee-transfer and callback attacks are covered by tests.

One account limit is not one-person identity. Multiple wallets, collusion, inference of public candidates and commitment-slot saturation remain possible. There is no account-count reward or unrestricted inflation farming, and no claim of complete Sybil resistance. Requesters who submit to their own job can recover their own reward but receive no new issuance.

## Self-hosted participant

Build the new library on Linux under `/srv/skew` using `python scripts/build_mining.py`; export the immutable path and SHA-256 from `artifacts/mining/native-build.json` as `ENGINE_MINING_LIBRARY` and `ENGINE_MINING_SHA256`. Set `PYTHONPATH=src`.

```sh
python scripts/mining_cli.py example > frozen-job.json
python scripts/mining_cli.py solve --job frozen-job.json > result.json
python scripts/mining_cli.py verify --job frozen-job.json --candidate agent-candidate.json
python scripts/mining_cli.py seal --job frozen-job.json --result result.json --contract DEPLOYED_ADDRESS --job-id 1 --miner YOUR_ADDRESS --secret-file private-reveal.json
python scripts/mining_cli.py reveal --secret-file private-reveal.json
```

`agent-candidate.json` contains a `path` array, so any agent/model may propose a result without sharing its API key. The CLI recomputes the result before sealing. The reveal secret is created exclusively with mode 0600; sealing cannot overwrite it or follow a final-component symlink. Commit output does not include the salt. Reveal output does; keep it private until the reveal window opens.

Transaction requests are **unsigned offline drafts**. The client does not currently authenticate an on-chain job or broadcast a transaction: read the frozen job/edges, code/token, deadlines and bond from the actual deployment, match the downloaded input, approve only the exact bond and sign with your own wallet. Do not treat an offline digest as a confirmed commitment, reward or receipt.

The console's Mining screen supports owner-scoped drafts, native search, result/download and restart persistence. Draft reward amounts are denominated in six-decimal test USDC for the planned Arbitrum Sepolia deployment. It displays zero confirmed earnings until actual chain evidence exists. No paid model call, GPU workload or token issuance occurs when clicking the native solver.

## Demonstration and release evidence

All compile/tests/browser work runs on the authorized Canadian server. `artifacts/mining` contains the pinned Solidity compiler/source hashes, immutable native library hash, sanitizer evidence, changed-code tests and actual UI checks. Compiled bytecode running in Py-EVM proves the mathematical contest/escrow behavior in a local EVM; it does not prove a public-chain deployment.

`prepare_mining_deployment.py --owner ADDRESS` reads two Arbitrum Sepolia RPCs, checks chain/token/nonces, estimates deployment gas and writes a source-bound **unsigned** deployment review. It never reads a signing key. A new public deployment requires owner authorization and independently reconciled inclusion/finality/code/token observations before changing the release's deployment status.

Existing DataPass deployment and x402 test-USDC transfer evidence are separate. They cannot be relabeled as a Machine Mining escrow, commitment or reward transaction. The next competition evidence is a new public mining deployment followed by one funded job, two participant commitments/reveals, finalization, exact reward withdrawal and independent RPC readback. Final HackQuest receipt/video are separately required.

Future task kinds can add execution bundling, deterministic transforms or constraint optimization, each with its own immutable input and bounded verifier. Free-form forecasts, subjective writing, arbitrary GPU usage, paid model-call loops, full EVM execution plans and token emission are deliberately unsupported in V1 rather than silently accepted by the route verifier.
