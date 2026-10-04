# Automatic settlement tracking

Approval starts a durable read-only workflow. The assistant displays progress
and reports completion only after `FILLED_FINALIZED` with `chain_verified=true`.
The existing chain verifier checks a successful canonical receipt, finality,
the order's trade event and recipient ETH balance against two independent RPCs.
A solver's `fulfilled` flag is not completion.

## Running workflow

1. The user signs the exact permit and trade in the wallet.
2. The existing journal commits submission uncertainty before order submission.
3. A server worker adopts signed/pending orders, including pre-upgrade records.
4. It checks settlement with a durable lease and bounded retry schedule. Provider
   failures retain the order and reservation, back off, and expose no raw errors.
5. The browser reads the cached tracking record every seven seconds. This adds
   no LLM calls and does not repeat RPC checks for each browser tab.
6. Finalized receipt verification yields “Swap complete” and the received ETH
   amount; proven expired/unfilled orders yield a distinct unsuccessful outcome.
7. Reopening the console restores the saved order. Closing the browser does not
   stop the server worker. No automatic replacement order is authorized.

The standalone Fuel portal owns its worker through application lifespan. Hosted
and self-hosted engine workers also reconcile bound Fuel requests. Fuel is charged
exactly once; the parent purchase reservation and separate approval remain intact.
Read-only agent keys can call `/api/engine/fuel/requests/{id}/watch`; signing and
submission still require the existing owner/signature boundary.

A crash after a lease claim is recoverable after 180 seconds. Healthy finality
checks run every 30 seconds; other pending checks every ten seconds. Source
failures back off from ten to 300 seconds. A terminal verified receipt is cached
and stops further chain reads. An order exposed for signing but whose submission
never reached the server is released only after finalized unfilled-expiry proof.

## Acceptance evidence

All execution tests ran on the owner-authorized Canadian server. The focused
suite passed 34 router, shared-budget and tracker tests, followed by the newly
added bound-worker charge-once test and two read-scope/HTTP regressions. Fixtures
cover worker restart, crash/lease recovery, concurrent workers, unavailable RPCs,
no signature/no resubmission authority, finality flags and unreceived submissions.
The browser fixture completed automatically, recovered completion after reload,
kept approval visible and submitted exactly once.

Production source backups are in
`/srv/skew/economic-machine-commerce-20261002/releases/automatic-tracking-before-20261004`.
The Fuel database was preserved across activation. Server evidence is in
`build/console-agent-20261004/evidence/auto-tracking-*`. Customer settlement
verification is recorded separately from fixture results.

## Receipt provider requirements

Both configured RPCs must serve historical receipts, the `finalized` block tag
and balances at a common finalized block. A working latest-block endpoint alone
is insufficient. Production uses the owner's private QuickNode
primary and the official `https://arb1.arbitrum.io/rpc` verifier. The previous
public secondary returned HTTP 403 for historical receipt reads; it was replaced
without weakening finality, changing the order or resetting its tracking journal.
Private endpoint credentials remain in the root-only server environment file.

Some public nodes also prune the old settlement-state trie while still serving
the finalized receipt. Holdings are therefore reconciled at the minimum of the
two providers' finalized heights, after verifying that the transaction is final
on both. Both must return the requested height, identical block hashes and
identical balances. The common balance block must be at or after settlement;
the unfinalized latest tip is never used. A balance below the approved minimum
remains in reconciliation, including when the wallet spent the funds afterward.
The receipt's block and the balance observation block are recorded separately.

The final focused router/tracker/budget suite passed **38 tests**, including
pruned settlement-state recovery, unequal finalized heights, balance/hash/height
disagreement, spent funds and a lagging finality source. See
`artifacts/automatic-settlement-20261004/auto-tracking-finalized-balance.txt`.

## Actual Arbitrum One completion

The customer signed and submitted an existing 10 USDC order. The read-only worker
completed it as `FILLED_FINALIZED`, with `chain_verified=true`, no source error
and no further scheduled check. The finalized trade delivered
**0.003706451488319571 ETH**. Both RPCs matched transaction block **511552130**
and the recipient's balance at common finalized block **511557545**.

[Transaction receipt](https://arbiscan.io/tx/0xa212a29b0742d002212742adae0499595c220b41dbbaa00764c067a08c44ddf4)

The production console displayed “Done. Your swap is finalized” and “Swap
complete” with the exact received amount. The saved record is
`artifacts/automatic-settlement-20261004/customer-auto-tracking-finalized.json`.
No additional signature or financial submission was made by the tracker or
during reconciliation. This is actual settlement evidence, separate from the
synthetic browser fixtures and unfunded test identities.
