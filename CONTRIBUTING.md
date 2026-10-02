# Contributing

Add a concrete economic primitive or adapter with bounded inputs, explicit authority, deterministic
failure behavior and a receipt that can be replayed. Keep model proposals separate from executable
programs. Keep read-only account access separate from order placement, wallet signing and settlement.

Preserve the versioned ISA, canonical fixed-point values and historical receipt identities. A semantic
change needs a versioned compatibility decision and negative coverage, not a silent rewrite of fixtures.
Tests should exercise stale state, incomplete information, concurrency, duplicate events, journal
corruption and ambiguous external outcomes where relevant. Generated repetitions and line-count padding
are not contributions.

Use Python 3.11 or later in an isolated environment. The maintainers run verification on their authorized
remote host. Contributor environments can run their own tests without accessing maintainer services:

```sh
python -m unittest tests.test_economic_machine tests.test_economic_spec tests.test_economic_inference
python -m unittest tests.test_engine_workspace tests.test_engine_portal tests.test_submission_evidence
node --check web/engine.js
```

New network adapters need deterministic fixture tests. Live public reads must be documented separately;
live money movement or signing is not part of the routine test suite. Do not commit secrets, live owner
databases, personal account snapshots or unredacted provider responses. Include the validation scope
and distinguish newly checked behavior from historical evidence in your pull request.
