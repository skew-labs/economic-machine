# Solution Mining — operations candidate

The initial Max-Cut research release is now extended with an executable C++ pipeline, an owner-local daemon/CLI, durable submission state and read-only dual-RPC reconciliation. This is a concrete production engineering increment, **not certification equivalent to Firedancer or ORE**. The mining contract and research token are not publicly deployed or issued. Existing commerce deployments do not prove this mining protocol is deployed.

## Runtime layout

```mermaid
flowchart LR
  A[Two RPC providers] --> B[Read-only round watcher]
  B --> C[Daily work reservation / private WAL]
  C --> D[Fixed binary input]
  D --> E[Capacity-eight SPSC]
  E --> F[C++ integer search]
  F --> G[Capacity-eight SPSC]
  G --> H[Exact verification]
  H --> I[Candidate journal]
  I --> J[Unsigned commit/reveal intent]
  J --> K[Owner's separate signer]
  K --> L[Owner-submitted transaction hash]
  L --> M[Dual-RPC receipt / finality reconciliation]
```

The native process has no RPC, provider-key, wallet or send-transaction interface. Before threads start, a Linux x86_64 seccomp allowlist rejects file open, socket/connect and exec syscalls. Only startup/runtime thread/memory/clock calls and inherited protocol pipes are permitted; `clone3` returns ENOSYS for glibc's clone fallback. The dynamic loader runs before this filter and remains part of the trusted boundary. A real subprocess test confirms file opening and socket creation return EPERM. The parent scrubs child environment and closes non-protocol descriptors. This is a narrow computation sandbox, not a claim of a fully audited validator sandbox.

Graph/algorithm bounds and exact answer scores are checked again across the process boundary. Network and storage wait outside integer search. Threads are created at startup; fixed calculation queues and solver arrays allocate no heap per candidate. Queue publication uses acquire/release atomics with explicit overflow backpressure. The final queue is drained after producer completion; EOF cannot discard a last published frame.

Frames are **local little-endian x86_64 ABI**, version 1, 24,240-byte requests and 72-byte results. They are not a public wire format. At most 128 jobs per process, eight queued graphs per stage and ten million edge visits per job are accepted. Parent timeout is 30 seconds. Optional search/verifier CPU affinity must fit the owner's allowed CPU set; no system-wide scheduler, NIC or NUMA settings are changed. A blocked/malformed worker fails closed. CPU affinity is a topology capability, not a measured speedup.

The same console's `SolutionLab` API routes search through this pinned sandboxed executable when `ENGINE_SOLUTION_PIPELINE`/`ENGINE_SOLUTION_PIPELINE_SHA256` are configured. Exact candidate verification remains a bounded trusted scalar C++ call. The receipt identifies the backend and executable hash. Two concurrent native subprocesses per Python service process are admitted; stalled workers cause subsequent requests to return busy, and completion/failure releases capacity. The parent daemon still owns read-only network access and journal I/O; no API/wallet credentials are inherited by native children.

## Protocol changes

- Removed the first-come global 64-commitment cap. A compiled-EVM regression funds 64 test wallets, then admits miner 65 and lets that miner reveal/win/claim. Counts/priority ordinals use uint256. Scoring and finalization do not iterate the submission population.
- One address commits once per problem. Qualification, one winner/problem, one claim and lifetime token cap are unchanged. Creating wallets does not multiply reward supply.
- The deployer becomes an immutable round operator, solely to authorize funded VRF requests and toggle admission pause. No transfer/upgrade function exists; operator key loss is an availability risk requiring a new deployment. Operator censorship is a disclosed trust boundary.
- Admission pause blocks new requests/commitments, but permits already-committed reveal, finalization and reward claims. It does not rewrite deadlines, seeds, scores or rewards.
- This removes one admission exploit, not all spam, validator/sequencer ordering or censorship. Attacker-paid persistent chain storage has no application-level total-submission ceiling. L2 throughput and long-term storage costs still need adversarial measurement.

## Chain consistency and recovery

Working rounds use the lower of two providers' recent heads minus four confirmations by default. Both providers must agree on the selected block, deployed bytecode hash and ABI calls. The block is reread after calls and before intent persistence/export. This is **recent canonical consistency, not finality or a cryptographic state proof**. Distinct provider hostnames do not prove independent backends.

Using only the `finalized` tag for a ten-minute commit window can miss the entire window. Accordingly a mined, successful commitment may enable reveal before finality, only after its canonical block falls within the separately agreed working anchor. This is a time-sensitive transition, not a claim of finalized economic effects.

Receipt outcome states are `UNKNOWN`, `PENDING_FINALITY`, `PENDING_REVERT`, `CONFIRMED`, `REVERTED` and `ORPHANED`. The private journal also uses `HELD` for unavailable/disagreeing/unverified reads. Confirmed/reverted require both providers' finalized anchors beyond receipt height and matching canonical block. Transaction hash, sender, destination, calldata, chain and zero value are independently bound. A tx hash alone is never success. Provider disagreement or receipt disappearance holds the intent. Even a formerly confirmed intent can regress to held state upon new evidence. Every reconciliation appends an observation atomically with its current-state update; earlier confirmations/failures remain available in the private journal.

Claims can refer to historical completed rounds even after a new round starts. Claim preparation reads the finalized round, exact winner/bits/score and unclaimed flag; it emits an unsigned owner-only claim. A confirmed claim additionally requires the exact mint Transfer event and mining Claimed event, and rereads finalized token issuer/cap/supply/owner balance. Finalized state may be up to one hour old for claim preparation; commit/reveal working state is limited to 120 seconds old. Current balance is a point-in-time observation and need not equal the mint if an owner subsequently transfers tokens.

The journal is an owner-private 0700 directory / 0600 SQLite database, `WAL` + `synchronous=FULL`. Work reservations and daily edge-visit limits transact atomically across connections and survive restart. Identity includes contract code hash, chain, round, seed, threshold, miner and problem. Reservations are not automatically refunded after crashes: this prevents repeated work/budget reset, but abandoned `RESERVED` jobs need explicit operator investigation.

Salt files are exclusive 0600 files, fsynced together with their directory before intent creation. A crash between secret persistence and intent insertion recovers and checks the exact original secret/binding. `HANDED_OFF` is committed before writing an owner's unsigned export. Export crashes remain held, never automatically return to unsigned or broadcast. The owner can inspect/reconcile the private journal; no replacement/retry command is supplied in this release. Preserve private state in owner-operated encrypted backups; this release does not supply off-host secret backups.

## Operator flow

Build `scripts/build_solution_pipeline.py` and `scripts/build_solution.py` on the authorized remote Linux host. Use proof hashes to set `ENGINE_SOLUTION_LIBRARY`/`ENGINE_SOLUTION_SHA256` for local exact verification. Create a private mode-0600 configuration file containing:

```json
{
  "rpc_a": "https://YOUR_FIRST_PROVIDER",
  "rpc_b": "https://YOUR_SECOND_PROVIDER",
  "contract": "REVIEWED_DEPLOYED_MINING_ADDRESS",
  "code_sha256": "EXACT_DEPLOYED_RUNTIME_SHA256",
  "miner": "OWNER_REWARD_ADDRESS",
  "directory": "/var/lib/skew-solution",
  "pipeline": "/opt/skew-solution/solution-pipeline-PINNED_HASH",
  "pipeline_sha256": "PINNED_EXECUTABLE_SHA256",
  "edge_budget": 100000,
  "daily_edge_limit": 10000000,
  "working_confirmations": 4
}
```

Do not paste provider credentials or salt files into chat, shared logs, public evidence or the hosted console. Keys stay in the owner's environment. The CLI never creates/uses a wallet key or sends raw transactions. RPC methods are positively allowlisted as read-only; embedded URL user/password, cleartext external transport and HTTP redirects are rejected.

```sh
PYTHONPATH=src python scripts/solution_operator.py --config owner.json run --watch
PYTHONPATH=src python scripts/solution_operator.py --config owner.json status
PYTHONPATH=src python scripts/solution_operator.py --config owner.json prepare --job JOB_HASH --action commit
PYTHONPATH=src python scripts/solution_operator.py --config owner.json export --intent INTENT_HASH --output owner-commit.json
# The owner reviews, signs and submits externally. No automatic execution occurs.
PYTHONPATH=src python scripts/solution_operator.py --config owner.json observe --intent INTENT_HASH --tx-hash SUBMITTED_HASH
PYTHONPATH=src python scripts/solution_operator.py --config owner.json reconcile --intent INTENT_HASH
PYTHONPATH=src python scripts/solution_operator.py --config owner.json prepare --job JOB_HASH --action reveal
PYTHONPATH=src python scripts/solution_operator.py --config owner.json prepare --job JOB_HASH --action claim
```

`status` contains only state counts, never private calldata/salts. The private SQLite database contains full work and intent records for owner's local tooling; a graphical owner-local job browser is not supplied by this increment. Prepare/export rechecks contract, round, seed, threshold, canonical anchor, permissions and a 60-second deadline margin. New work polls every 10–300 seconds with zero LLM calls. Candidate-only operation is separate from transaction authority. Once-exported payloads still require the separate signer to enforce the approved contract/chain/call, gas spending, nonce and deadline before actual broadcast.

`deploy/solution-miner.service.example` supplies a dedicated-user service profile: no privilege escalation, read-only system, private temp/devices, no capabilities, restricted address families, 256MB memory, one CPU quota and sixteen-task maximum. It is a template, not an installed live mining service. Configure reviewed paths and private owner state before use. The Python daemon has RPC network access; the native child installs its own seccomp filter. This does not reproduce Firedancer's complete privilege separation or validator responsibilities and does not claim zero syscalls.

## Evidence and remaining production gates

`artifacts/solution-operations` records compiled artifacts, native ASan/UBSan and TSan, concurrency tests, failure/recovery tests and a 120-second fixed-frame soak. Initial integration failure was a missing mining-library setting; only that failed connection test is repeated after correcting the environment. Successful unchanged contract/native tests are retained rather than gratuitously rerun.

Focused validation covers 40 distinct current cases across the retained runs. A separate public-browser check exercises only the newly changed console/backend connection and receipt download, preserving earlier responsive/UI evidence. Release manifests remain size-bounded and now admit at most 1,000 files to accommodate this actual source/evidence corpus; individual path, symlink, duplicate and file-hash checks remain enforced.

The final sandboxed executable completed **102,432 jobs in 120.006 seconds**, zero frame/order/score failures. Native search p50 **0.272ms**, p99 **0.696ms** at 100,000 edge visits; 16-job process round-trip p50 **6.496ms**, p99 **16.159ms**, including process spawn. Peak child rusage reports 43,856KiB (process-lifecycle accounting, not a precise steady-state native RSS measurement). This synthetic fixed workload excludes RPC, VRF, operator approval, signing, chain inclusion and finality. It is not equal-cost GPU/AI comparison or a Firedancer/ORE benchmark. The pre-seccomp baseline is retained separately.

The final TSan run initially failed at startup due to this kernel's address-space mapping. The same compiled concurrency test passed with ASLR disabled for that test process alone (`setarch x86_64 -R`); the production worker retains normal ASLR. ASan/UBSan and real sandboxed process tests also passed. This is measured test coverage, not a formal race-freedom/security proof.

Still required before public issuance: independent contract/native security review, funded real VRF subscription/consumer registration, public testnet lifecycle with separately approved signatures, 24h/7d soak and restart chaos, multiple operators under actual L2 congestion, encrypted off-host private-state restore, reward/difficulty incentives and useful-task economics. No audit, public mining deployment, mainnet issuance, paid GPU/API job, automatic signer, AF_XDP, FPGA or SIMD improvement is claimed.

A public read-only two-provider probe observed matching working-block hashes on Arbitrum Sepolia and a finalized head roughly 855 seconds behind observation time. This demonstrates why ten-minute commit preparation cannot be driven solely from finalized state. Earlier unavailable read samples are retained. The full C++ → private commitment → local EVM reveal → claim → mint-event and balance readback integration uses disposable accounts, a mock VRF coordinator and simulated finality; it is not a public testnet transaction. The resource-restricted service smoke only checks CLI startup, not continuous funded mining.

Reference implementations reviewed for operational responsibilities: [Firedancer](https://github.com/firedancer-io/firedancer), [ORE](https://github.com/regolith-labs/ore). No code is copied from either and their production maturity is not inherited by citing them.
