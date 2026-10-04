# Solution Mining operations

The C++ worker pipeline connects to an owner-local CLI, durable submission journal
and read-only dual-RPC reconciliation. Search, verification, signing and settlement
remain separate authority boundaries.

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

## Protocol limits

- Commitments have no global first-come cap. Counts and priority ordinals use uint256. Scoring and finalization do not iterate the submission population.
- One address commits once per problem. Qualification, one winner/problem, one claim and lifetime token cap are unchanged. Creating wallets does not multiply reward supply.
- The deployer becomes an immutable round operator, solely to authorize funded VRF requests and toggle admission pause. No transfer/upgrade function exists; operator key loss is an availability risk requiring a new deployment. Operator censorship is a disclosed trust boundary.
- Admission pause blocks new requests/commitments, but permits already-committed reveal, finalization and reward claims. It does not rewrite deadlines, seeds, scores or rewards.
- Uncapped admission does not prevent spam, validator/sequencer ordering or censorship. Attacker-paid persistent chain storage has no application-level total-submission ceiling. L2 throughput and long-term storage costs still need adversarial measurement.

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

## Verification

`tests/test_solution_operations.py` exercises persistence, concurrent reservations,
restart recovery, RPC disagreement and commit/reveal/claim reconciliation. Native
conformance tests cover queue backpressure, frame ordering, integer scores and the
Linux syscall sandbox. Sanitizers supplement these checks; they are not a security
proof. Benchmarks must report search time separately from RPC, signing, inclusion
and finality.

An operator must independently review deployed code, VRF registration and funding,
key recovery, restart handling and reward economics before enabling public issuance.
Mocks and isolated EVM results do not establish a live VRF or token deployment.
