# Solution runtime: production release work

This release adds durable disaster recovery and sustained qualification to the C++ solution runtime. It does not declare the token protocol audited, deploy it, mint a token, fund VRF or authorize a wallet. The C++ binary is unchanged from PR5; new work hardens its operation and recovery boundaries.

## Implemented boundaries

- A mining backup takes a SQLite write reservation, copies the committed WAL state, validates every commitment against its private salt and transaction binding, and packs the secrets into the snapshot database. AES-256-GCM authenticates the complete archive. The encryption key is never archived or printed.
- Secret packing avoids the previous small attachment-count ceiling. Entries are bounded by the protocol's 160,000 possible per-miner problem submissions and a 128MiB packed database limit. Larger journals fail closed and require an explicit archival/retention design. The secret pack is added only to the snapshot, never to the live database.
- Restore authenticates and validates in private staging, writes a held state into both the database and a private marker, then publishes with Linux `renameat2(RENAME_NOREPLACE)`. A competing empty destination cannot be overwritten. Removing the file marker cannot unlock the restored database.
- All restored intents remain held. Status and transaction reconciliation still work; new search reservations, new intents and unsigned handoffs are blocked. This is deliberate protection against a stale backup and two active miners. No automatic recovery-unlock command is included.
- The sustained qualification runner pins both executable and runner-source hashes, independently replays every score, uses bounded latency histograms, persists atomic checksummed checkpoints, excludes downtime and unsaved crash work, and rejects concurrent runners. Failed score/frame runs require a new run rather than silently starting over.
- Deliberate native-worker termination is followed by new verified batches. A parent process crash is tested separately with a real service kill and checkpoint restart. Requested shutdown discards an interrupted batch and preserves a paused checkpoint.
- Qualification has a 5GiB remaining-disk floor. The service used on Canada has a 256MiB memory limit, 25% of one CPU, sixteen-task maximum, low scheduling priority, a read-only system and no network address families except local Unix sockets. This prevents an unbounded synthetic test from competing with live application services.
- Public qualification evidence is a separate allowlisted metadata export with mode 0644. Private checkpoints remain 0600. This fixes the actual portal 503 caused by including owner-only checkpoint copies in a publicly verified manifest, without changing permissions on salts, backups or keys. Unknown fields, invalid hashes and paths outside the public evidence directory are rejected.
- Real VRF preflight reads the documented Arbitrum Sepolia coordinator through two distinct RPC hosts at one recent canonical block, compares code and subscription state, checks the subscription owner and an explicit LINK floor, and rechecks the anchor. The floor is not a promise that volatile VRF fees will fit. These reads are not a cryptographic state proof.
- Optional unsigned constructor data binds only the official testnet coordinator, key hash and supplied subscription. It remains a review template without nonce, gas or signing authority. Mainnet is rejected.

Official network parameters: [Chainlink supported networks](https://docs.chain.link/vrf/v2-5/supported-networks). Subscription return types: [official interface](https://github.com/smartcontractkit/chainlink-brownie-contracts/blob/main/contracts/src/v0.8/vrf/dev/interfaces/IVRFSubscriptionV2Plus.sol).

## Owner recovery procedure

Run on the trusted Linux host as the owner of the private miner directory, using a separately stored 32-byte key file with mode 0600. Never put the key in an argument or public report.

```sh
PYTHONPATH=src:scripts .venv/bin/python scripts/solution_recovery.py \
  --key-file /private-owner-path/recovery-key snapshot \
  --directory /private-owner-path/miner \
  --output /private-owner-path/new-backup.encrypted \
  --off-host-config /private-owner-path/admitted-backup-destination.json
```

The optional off-host profile uses the existing ciphertext-only SSH transport. A successful test must observe a different host identity, check saved host keys, retrieve the exact ciphertext and authenticate/restore it. A local round trip alone leaves `off_host_verified=false`. The backup server receives no recovery key. A destination and separate disaster-recovery key custody remain owner configuration requirements.

Restore into a new private directory:

```sh
PYTHONPATH=src:scripts .venv/bin/python scripts/solution_recovery.py \
  --key-file /private-owner-path/recovery-key restore \
  --input /private-owner-path/retrieved-backup.encrypted \
  --destination /private-owner-path/new-held-miner
```

Before any unlock, fence the old machine and signer, reconstruct later work-budget reservations, reconcile every known transaction through finalized chain state and account for handoffs whose transaction hash is missing. Merely deleting `RECOVERY_HOLD` does not satisfy these requirements. This PR intentionally provides a held recovery, not an unverified operator bypass. An owner-reviewed recovery migration is still required to resume actual mining after disaster recovery.

## Qualification procedure

```sh
PYTHONPATH=src:scripts .venv/bin/python scripts/qualify_solution_runtime.py \
  --seconds 86400 --inject-every 1000 \
  --checkpoint /private-owner-path/qualification-24h.json
```

Use the supplied restricted service profile with reviewed paths. This is one bounded run, not a recurring scheduled automation. A checkpoint with `RUNNING` is in progress; only `COMPLETE` with sufficient accumulated duration and zero validation failures passes its duration gate. Reuse the exact same executable, script and parameters to resume after a stopped process. A script/hash change requires a new qualification. The synthetic fixed graphs do not prove real-chain performance, multi-user isolation, useful-work economics, or equivalence to Firedancer/ORE. Histogram percentiles are upper bounds, not exact percentiles.

## Public-chain launch sequence

1. Independent review of the contract and native/operations boundary, with findings resolved against a named commit.
2. Owner-created/funded VRF v2.5 testnet subscription. Run `prepare_solution_launch.py --owner ADDRESS --subscription ID --output NEW_REVIEW.json` and review all remaining gates.
3. Separately approve gas, nonce, creation data and signature; deploy the mining consumer. Record the actual deployment receipt and read back immutable parameters/code at a matched finalized block.
4. Subscription owner registers that deployed consumer. Verify the authorized consumer and subscription funding at a new anchor. A test coordinator is not valid evidence for this step.
5. Separately approve a bounded round request. Observe the coordinator's real request/fulfillment and the resulting round windows without a seed fallback.
6. Miner computes a candidate, privately saves its commitment, then owner separately approves commit/reveal. Finalize only after the real deadline. Claim only after independently re-reading the winner and finality; verify mint events, supply cap and the miner's token balance.
7. Complete 24h and then seven-day operation under actual target load, off-host recovery with independently recoverable key custody, multiple operators and realistic chain/RPC failures. The current throttled synthetic run is one component of this evidence.

## Current release evidence

See `artifacts/solution-launch/validation.json`, focused test logs, the real-coordinator preflight, the three-minute crash/restart qualification and the timestamped 24-hour checkpoint observation. Source and evidence hashes support reproducibility; they do not replace external review or make an unfinished run complete. Public token issuance stays closed. Existing agent commerce and its separate wallet authority are unaffected.
