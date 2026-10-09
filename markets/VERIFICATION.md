# Public source verification

The portable publication snapshot was built and tested on the authorized Linux
builder. No model calls, chain transactions or new operating mandates were used.
This checks source packaging; historical live settlement is documented separately
in the research companion.

| Suite | Passing test invocations |
| --- | ---: |
| Solana v6 wide SBF and native boundaries | 70 |
| Solana v7 wide SBF and native boundaries | 75 |
| Bounded DAG evolution and reference evaluator | 25 |
| Native evaluation, profile history and SBF connection | 16 |
| Solana v7 production initialization | 10 |
| Arbitrum journal, observer, transport and coordinator | 44 |
| Arbitrum Solidity suites, fuzz and invariants | 59 |

The Rust/Solidity matching and accounting sources are unchanged from the measured
versions. Portable default paths, shared C++ includes, normalized strategy fixtures
and capacity-aware page tests replace private work-area dependencies. None removes
an admission, oracle, session, fee or conservation check. Compiler and artifact
identities are in [source provenance](source-provenance.json).

Public-tree verification checks included files, relative documentation links and
credential patterns. It is a bounded publication scan, not an external security
audit or an erasure of prior Git history.
