# Unified operations console

## Design, pass 1

Reference: official TypeSafe login and the published, first-hand Jev Playground
capture at https://flaviocopes.com/images/jev/playground.png. The authenticated
console has not been inspected. Adopt its compact navigation and adjacent
input/result workspace; do not reproduce its brand or illustrations.

Palette: Paper #fafafa, Surface #ffffff, Ink #171717, Muted #737373,
Border #e5e5e5, Signal #236747. System sans for labels, monospace for amounts,
policy JSON and receipts. English labels, 220px sidebar, single 1200px content
column, aligned data rows and a two-column execution workspace.

```
sidebar       | account identity / environment
Overview      | balances | positions | open orders
Connections   | source / freshness / sync schedule
Execution     | approved policy + order | immutable plan / outcome
API usage     | reported tokens and cost, provenance
API keys      | existing access controls
Funds         | existing x402 limits
Activity      | existing delivery and payment records
```

## Design, pass 2

Keep the existing wallet login, key issuance, funds and receipt dialogs. Remove
the second console destination: `/engine` redirects to `/console`. Use sparse
tables and one empty state per view; never invent positions, performance charts
or live balances. Read-only recorded evidence remains explicitly separate from
authenticated accounts. Keys stay in the user's environment; the hosted console
accepts only references within that owner's credential namespace.

## Runtime contract

LLM inference is outside monitoring and execution. Durable sync jobs have
generation checks, exclusive expiring leases, bounded retries and backoff.
Broker adapters implement instrument, account, submit, query and cancel ports.
The first execution adapter is Binance Spot LIMIT; USD-M futures are read and
reduce-only LIMIT execution in one-way mode. No withdrawal, leverage change,
market order or arbitrary broker URL is an execution primitive.

Every live order needs an immutable turnover policy, fresh account evidence,
instrument filter validation, a hash-bound owner approval and an operator live
gate. Transport timeouts retain reservations and are reconciled by the original
client order ID. Neither HTTP success nor an order ID proves a fill. Terminal
responses record actual filled quantities and quote amounts; commission records
remain explicitly unverified until imported from the venue's trade history.

The public production services keep live transmission disabled. Implementing
the capability does not authorize sending a trade. Venue turnover budgets do
not share or route through the external data/compute x402 ledger.
