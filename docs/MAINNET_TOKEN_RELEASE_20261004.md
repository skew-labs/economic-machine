# SKEW product-backed mining release

This release prepares Arbitrum One deployment. It does not broadcast or hold a customer key.

A single SkewLaunchBundle creates SkewDataPass, SkewArtifactMining, and its SKEW ERC-20. Initial supply is zero, the lifetime cap is 160,000 SKEW, and each accepted job can mint exactly one SKEW to the winning miner. There is no owner mint entry point. These constants are a versioned launch design, not a statement about token value or earnings.

Publisher-admitted work freezes integer routing inputs, licensed source rights, reward rules and deadlines. Miners commit before revealing. Verification recomputes the bounded route and ties a unique result to its submitter. The winner claims only after an active DataPass release binds the same artifact, terms and rights. Other work requests may pay requester escrow; they are not token emissions.

## Trust and release boundaries

Publication and legal rights are publisher attestations, not cryptographic proof of copyright. An operator can withhold admission or publication. This is not permissionless ORE-equivalent production or an independent security audit. The token has no established liquidity or value. Existing VRF solution research is separate and remains gated.

Use the remote-only `scripts/compile_artifact_mining.py`, then `scripts/prepare_datapass_launch.py prepare --help`. A short-lived review binds initcode, owner, nonce, chain and gas cap. Only the owner wallet signs. On an ambiguous response retain the transaction hash and reconcile; never automatically deploy again. Reconciliation verifies finalized creation and deployed code/configuration with two RPCs. Review gas is capped at 0.0005 ETH; no USDC is spent deploying.

## Acceptance evidence

`artifacts/amazon-completion/pr12/` contains remote compilation, contract/rights tests, reader tests and wallet checks. Tests use an in-memory EVM and mocked read adapters; the launch quote is read-only Arbitrum mainnet evidence. None is a mainnet issuance receipt. Actual mainnet addresses, token issuance, purchase and delivery require separate finalized receipts.
