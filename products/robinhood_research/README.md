# Robinhood Chain research adapters

The [library](../../src/economic_machine/robinhood_research/) collects hash-bound
headers, receipts and available traces under a shared source gate. It preserves
provider cooldowns, immutable source records and explicit trace gaps.

[run.py](run.py) and [bridge_worker.py](bridge_worker.py) are explicit operator
entry points. [Snapshot scouting](snapshot_scout.py) and
[bounded header branching](snapshot_branch.py) can locate candidate history
tables. A located tar header is not an extracted transaction or searchable wallet
history. Access failures and missing tables remain unresolved.

Cross-chain messages require matching source and destination evidence. A direct
Arbitrum/Robinhood message differs from an Ethereum-mediated route. A candidate
message, identical amount or provider label is not proof of common ownership or
the same funds passing through a private exchange ledger.

No archive dataset, active grant, credentials, private dashboard or service
installation is bundled. Historical deadlines and spending checks remain in
place. This is research integration source, not a claim that complete Robinhood
history or production tracing has been achieved.
