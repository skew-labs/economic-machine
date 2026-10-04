# Smart contracts

Solidity source is the contract interface and implementation. Generated ABI,
bytecode, compiler receipts and deployment records are not checked into Git.
Availability of source does not imply an audit or an active deployment.

## Protocol contracts

| Source | Responsibility |
| --- | --- |
| [SkewDataPass.sol](SkewDataPass.sol) | Immutable product versions, license purchase and entitlement records |
| [SkewArtifactMining.sol](SkewArtifactMining.sol) | Winning work, matching DataPass publication and bounded reward eligibility |
| [SkewLaunchBundle.sol](SkewLaunchBundle.sol) | Construction and wiring of the DataPass/artifact-mining bundle |
| [MachineCommerceEscrow.sol](MachineCommerceEscrow.sol) | Requester-funded commerce escrow and settlement |
| [MachineMining.sol](MachineMining.sol) | Frozen route jobs, bonded commit/reveal, deterministic scoring and pull rewards |
| [SkewSolutionMining.sol](SkewSolutionMining.sol) | VRF-backed solution rounds and the capped `SkewSolutionToken` |
| [SolutionMining.sol](SolutionMining.sol) | Separate research protocol and `SolutionResearchToken` |

These protocols have different reward and authorization rules. Requester-funded
rewards, artifact-publication rewards and solution-token issuance are not one pool.
The [architecture](../docs/ARCHITECTURE.md) describes their Engine boundaries.

## Test contracts

[TestCommerceToken.sol](TestCommerceToken.sol),
[TestEIP3009Token.sol](TestEIP3009Token.sol),
[DataPassAdversaries.sol](DataPassAdversaries.sol),
[MiningAdversary.sol](MiningAdversary.sol),
[SolutionVRFMock.sol](SolutionVRFMock.sol) and
[SkewVRFMock.sol](SkewVRFMock.sol) supply disposable tokens, hostile callbacks and
mock coordinators. Mock VRF callbacks do not verify cryptographic randomness.

## Compilation and verification

The compiler helpers under `scripts/compile_*.py` pin Solidity 0.8.24, verify the
compiler digest and write local output under ignored `artifacts/`. Each family
has its own settings and source-hash records; satisfy the helper's documented
compiler prerequisites before invoking it. Python contract tests consume freshly
compiled output. Do not substitute a historical deployment report for a build.

| Family | Build helper | Test module |
| --- | --- | --- |
| Commerce / EIP-3009 | [compile_contracts.py](../scripts/compile_contracts.py) | [test_escrow.py](../tests/test_escrow.py), [test_payments.py](../tests/test_payments.py) |
| DataPass | [compile_datapass.py](../scripts/compile_datapass.py) | [test_datapass_contract.py](../tests/test_datapass_contract.py) |
| Artifact mining | [compile_artifact_mining.py](../scripts/compile_artifact_mining.py) | [test_artifact_mining.py](../tests/test_artifact_mining.py) |
| Route mining | [compile_mining.py](../scripts/compile_mining.py) | [test_mining.py](../tests/test_mining.py) |
| Research solution rounds | [compile_solution.py](../scripts/compile_solution.py) | [test_solution.py](../tests/test_solution.py) |
| Solution token and signer | [compile_solution_mainnet.py](../scripts/compile_solution_mainnet.py) | [test_solution_mainnet.py](../tests/test_solution_mainnet.py) |

Compilation does not deploy, sign, fund a subscription or mint a public token.
Keep deployment authority and private signing material outside the source tree.
