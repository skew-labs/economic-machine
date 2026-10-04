# Unified operations console

`/engine` redirects to `/console`. Connections, agents, shared limits, tasks and
receipts use the same workspace and authority journal. Account state and receipt
views are projections of that runtime; they are not separate execution systems.

## Observation and execution

Durable synchronization jobs use connection generations, exclusive expiring
leases, bounded retries and backoff. Disconnecting an account invalidates an
in-flight stale refresh. Network I/O stays outside database write transactions.

Broker adapters expose instrument, account, submit, query and cancel operations.
Supported order primitives include Binance Spot LIMIT and one-way USD-M
reduce-only LIMIT. Withdrawal, leverage changes, market orders and arbitrary
broker URLs are not general execution primitives.

Every live order requires a turnover policy, fresh account evidence, instrument
filter validation, hash-bound owner approval and an enabled operator gate.
Transport timeouts retain reservations and reconcile the original client order
ID. Neither HTTP success nor an order ID proves a fill. Terminal responses record
observed filled quantities; commissions remain unverified until venue trade
history supplies them.

LLM inference runs outside monitoring and financial execution. A model can propose
an action or explain an exception; it cannot authorize that proposal itself.
Venue turnover accounting is separate from the x402 ledger for external purchases.

## Integration points

- [Engine architecture](ENGINE_ARCHITECTURE.md): compiler, kernel and journal.
- [Agent control](AGENT_CONTROL.md): shared policies and agent-bound keys.
- [Console access](CONSOLE.md): identity, accounts and owner-only actions.
- [Self-hosting](SELF_HOSTING.md): installation and endpoint configuration.
