# Core extraction and verification provenance

The original owner-developed source is `/Users/heoun/gwdc-tron-b-planning/src/economic_machine`.
This extraction copies the chain-independent `compiler`, `kernel`, `state`, `spec`, `transitions`,
`runtime`, `inference`, `execution_evidence` and `settlement` modules. The pre-existing `values` and
`journal` modules were byte-identical to the original before formatting. Imports/UTC aliases and two
nested conditions were formatted to this repository's lint rules; receipt semantics remain pinned.

The imported suites are `test_economic_machine`, `test_economic_spec` and `test_economic_inference`.
Their cases cover ISA versions 1–3, guarded and graph branches, exact receipt hashes, expiry, unit
checks, shared capital reservations, execution locks, pause/revision interactions, tamper rejection,
out-of-order observations, dependency-based reevaluation and inert model proposals. The source
repository's larger TRON portfolio, contract, collector and allocation suites are not copied or claimed.

`cases/economic_isa_vectors_v1.json` contains 195 portable transition vectors. Count these as vectors,
not generated “implementation lines.” Synthetic state and program fixtures are labelled separately
from real Sepolia purchase evidence.

The new `machine_engine` modules wrap the core with user-owned connection metadata, read-only
adapters, usage accounting and an authenticated loopback API. They do not grant original kernel
intents a new signer or an external order-execution adapter. x402 commerce remains an independent
settlement path for external services; exchange observations and future exchange trades use the venue.

All verification runs on Canada under `/srv/skew/economic-machine-commerce-20261002`. The Mac
edits source and orchestrates remote checks. Remote source/evidence hashes and test logs are kept
under `artifacts/submission`; private runtime DBs and credentials are excluded from publication.
