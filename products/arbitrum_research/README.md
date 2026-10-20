# EVM history extraction and wallet search

The [Arbitrum research library](../../src/economic_machine/arbitrum_research/)
contains bounded source transport, snapshot/freezer readers, graph shards,
verified cold storage, source scheduling and read-only wallet search. Robinhood
adapters reuse the same evidence boundaries.

The [freezer decoder](../../native/freezer_decode/) uses the exact public Nitro
geth dependency pinned in its module file. It checks header identity, transaction
and receipt roots, log bloom, ordering and page-local parent continuity. These
checks do not independently establish consensus finality. Freezer records do not
supply missing internal traces; trace coverage must remain separate.

Graph schema two retains repeated transaction hashes as distinct block/index
occurrences. Conflicting source assertions remain inspectable. Failed
transactions and unresolved sender claims do not create successful value edges.
Search preserves coverage, truncation and source-discrepancy metadata.

Before local derived duplicates can be retired, the archive path requires full
object readback, hashes, reconstruction evidence and address-query parity.
Original source receipts, uncertain outcomes and journals remain preserved.

The product scripts are integration entry points, not automatically installed
services. Historical operating policies contain expired fixed deadlines and
require their original authority hashes. Example filesystem defaults are under
`/srv/economic-machine`; no operator state, cloud identity or active grant is
distributed here.

Build the optional decoder separately on an authorized Linux host with Go 1.24+:

```sh
cd native/freezer_decode
go test ./...
go build -o ../../build/freezer-decode .
```

Successful fixture tests are not a complete-history or sustained-throughput
claim. Current balances, CEX private ledgers and complete cross-chain attribution
cannot be inferred from a partially populated graph.
