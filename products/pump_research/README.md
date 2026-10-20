# Solana public-evidence research

The [research library](../../src/economic_machine/pump_research/) provides
bounded CAR decoding, address and signature index readers, transaction evidence,
wallet graphs, source coverage, cold-object verification and research queues.
[tools.json](tools.json) describes the read-only search interface.

An external address index supplies candidate transaction locations. Original
bytes, content hashes, signatures, successful instructions and address membership
must be checked before recording a verified fact. Processed observations,
provider claims, canonical records and independent consensus finality remain
separate. Missing history is retained rather than treated as a clean wallet.

Custody and cross-chain modules preserve unresolved ownership and private-ledger
gaps. Matching amounts, times or exchange labels cannot identify the same funds
through a centralized exchange. A bridge-index lead is not a verified redemption.

Install the optional `research` dependencies for offline fixtures. On Linux the
archive readers also use the system zstd library. Collector integration expects
the generated Yellowstone protobuf bindings under `vendor/yellowstone` in an
explicit collector root. Those externally generated bindings are not bundled;
the export integration test reports a skip when they are absent.

Operational grants, raw datasets, private desk UI, evidence journals and service
units are excluded. Research authority remains hash-bound and scoped; publishing
the code does not activate a historical job or grant model spending permission.
The included historical defaults use example installation paths under
`/srv/economic-machine`; a live integration needs its own reviewed configuration.

The library does not claim complete Solana history, current balances, common
owners, bundle participation, independent profitability or live execution.
