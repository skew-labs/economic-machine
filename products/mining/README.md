<img src="../../site/assets/app-mining.svg" width="64" alt="Mining">

# Mining

Submit a verifiable result, not a bill for GPU time or model tokens. Search runs outside the
signer; deterministic verifiers check candidates against the frozen job.

## Choose the reward protocol

| Contract | Work / reward |
| --- | --- |
| [SkewArtifactMining](../../contracts/SkewArtifactMining.sol) | Winning route work plus matching DataPass publication; bounded SKEW issuance. |
| [MachineMining](../../contracts/MachineMining.sol) | Frozen route optimization; requester-funded escrow. No native issuance. |
| [SkewSolutionMining](../../contracts/SkewSolutionMining.sol) | Max-Cut research rounds with VRF and bounded issuance. Separate from product-backed work. |

These are separate protocols. Publication is not proof of a customer purchase; an issued token
is not cash income. No algorithm or API provider is guaranteed to be profitable.

## Build and search

Use the CMake build in the root README, then set the library and its exact SHA-256:

```sh
export ENGINE_MINING_LIBRARY="$PWD/build/native/libmachine_mining.so"
export ENGINE_MINING_SHA256="$(sha256sum "$ENGINE_MINING_LIBRARY" | cut -d ' ' -f 1)"
.venv/bin/python scripts/mining_cli.py example > frozen-job.json
.venv/bin/python scripts/mining_cli.py solve --job frozen-job.json > result.json
```

This searches locally; it does not commit on chain or mint. [Protocol semantics](../../docs/MACHINE_MINING.md).

| Area | Source |
| --- | --- |
| Native search and exact score | [mining.cpp](../../native/src/mining.cpp), [solution.cpp](../../native/src/solution.cpp) |
| Linux bounded worker / sandbox | [solution_pipeline.cpp](../../native/src/solution_pipeline.cpp) |
| Python route worker | [mining.py](../../src/machine_engine/mining.py), [CLI](../../scripts/mining_cli.py) |
| Replay-checked product construction | [work_artifacts.py](../../src/machine_commerce/work_artifacts.py), [CLI](../../scripts/work_artifact.py) |
| Owner-local submission / recovery | [solution_signer.py](../../scripts/solution_signer.py), [operations](../../docs/SOLUTION_OPERATIONS.md) |

Production operation also requires deployed contracts, configured randomness for the VRF protocol,
gas funding, private commitment/salt storage and independently reviewed release parameters.
The native pipeline never receives wallet or provider credentials.
