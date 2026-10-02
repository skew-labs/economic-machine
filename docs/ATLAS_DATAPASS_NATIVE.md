# Atlas, DataPass and native Engine

## Product and design

Pass 1: paper #fafafa, white #ffffff, ink #171717, slate #536779,
signal #236747. DM Sans headings/body, monospace only for hashes and amounts.
Keep the existing skew mark and NVIDIA Inception membership attribution.
The landing leads with working tools: Atlas market table, SiteLens calculator,
DataPass dataset releases and the existing Engine console. No invented charts,
transactions, coverage or affiliation with upstream data providers.

Pass 2: replace the fixture supplier hero with real dated APAC observations.
Use a compact Products menu and separate working pages, not repeated marketing
cards. Source freshness, unit and list-price scope stay adjacent to the result.
Preserve the existing comparison and payment evidence as secondary resources.

## Data and rights

Atlas collects official Azure public retail meters and AWS public bulk prices on
the owner-selected Canada host. It records request URLs, raw SHA-256, retrieval
time and effective dates. Public list prices do not establish capacity, private
discounts or executed transactions. Price/GPU is computed only for explicitly
mapped hardware; unknown GPU counts remain null. Partial collection is explicit.

DataPass binds a dataset version, content root, terms hash, seller, price and
license duration. An ERC-721 is a transferable access license only when its
published terms permit transfer; it is not ownership of a hyperscaler's data or
hardware. Source rights are supplier attestations, never cryptographic proof of
copyright ownership. Unreviewed third-party data cannot become an admitted sale.
The first own product is an original derived APAC compute report, not Silicon
Data content. Raw upstream archives remain research inputs.

Payments for external data remain separate from exchange trades. The existing
x402 adapter supports separately configured external resources; the APAC product
is not enabled for another live x402 sale. DataPass adds optional
on-chain access ownership and transfer. RPC-backed entitlement reads require the
configured chain and runtime bytecode hash, finalized ownership, terms and
expiry. A fresh wallet challenge proves control; an address alone is insufficient.
Off-chain bytes are not made atomically deliverable by minting an NFT.

Secondary sales bind the holder, price, nonce, version and terms. Payment and
license ownership move atomically; cancellation, relisting and a free transfer
invalidate old quotes. Resale preserves the original expiry. Readers fetch
entitlement and license fields at the same pinned finalized block on both RPCs;
wall-clock expiry prevents finality lag from prolonging access.

Atlas's first public snapshot is deliberately nonexclusive and publicly
inspectable. The token records paid version/terms/access entitlement; it does
not make public facts confidential or create exclusive provider-data rights.
Immutable releases live under `artifacts/atlas-release/versions/<content>.json`.
Requests can pin `?version=<content>` so a new collection never substitutes
another version for an existing license.

## Native core

The C++20 path uses fixed-width amounts, fixed state slots, bounded power-of-two
SPSC rings, cache-line separation, sequence checks and fixed decision receipts.
It evaluates price age, deadline, available balance, exposure, minimum notional,
size increments, slippage and turnover without allocating or calling a model.
The native result is a candidate and cannot grant signing or dispatch authority.
The Python authority, approval, journal and venue reconciliation boundary remains.

CPU topology is discovered rather than assumed. Affinity and huge-page advice
are opt-in per process; no production IRQ, NIC, governor or kernel tuning occurs.
Benchmarks include warmup, machine/compiler details, distributions and a separate
cross-thread queue test. Kernel latency is not exchange or Arbitrum latency.
AF_XDP/SmartNIC/FPGA and zero-copy external feeds are future ports, not implemented
capabilities. Line counts exclude fixtures, generated data, logs and binaries.

The numerical ISA has 26 opcodes, 32 registers/state fields and a 128-instruction
limit. Static analysis checks initialization, unit compatibility and a single
terminal candidate/abstention. Scaled multiplication/division truncate toward
zero and are for signals, not authoritative accounting. The order gate and
depth quote use conservative fee/cost rounding. The depth book invalidates on
gaps/crossed books and refuses quotes outside retained depth. C++ capital holds
are in-memory candidates; durable spending authority remains the Python journal.

The public native Playground evaluates supplied synthetic state. Its `valid`
field is caller input, not an oracle attestation or a connected account read.

Financial primitives include signed absolute/negation, score clamping, lot/tick
floor and ceiling, conservative purchase cost, sale proceeds and fee reserves.
Negative rounding quantities, nonpositive steps/prices, inverted clamp bounds,
fees outside 0–100% and integer overflow abort. These calculations can participate
in candidate rules but cannot replace independent durable execution approval.

## Verification chain

The release manifest hashes source, collected data, compiler output, test logs
and measurements. A standalone verifier reconstructs the hashes and derived
statistics, rejects path escapes and altered artifacts, and compares the public
manifest against a second independent read. A self-consistent hash proves file
integrity; it does not prove an observation's truth or production certification.
Arbitrum deployment and real purchase require their own receipt and finalized
readback; EVM tests and unsigned transactions are explicitly separate evidence.

## Reproduce on the remote host

Use a clean Python environment with the `verification` and `atlas` extras and
GCC 13+ with C++20. The controlled run uses pinned solc 0.8.24 described in the
contract compiler evidence. Public collection is read-only and leaves raw input
archives in the ignored private directory.

```sh
PYTHONPATH=src python scripts/collect_atlas.py --aws
PYTHONPATH=src python scripts/audit_atlas_sources.py
PYTHONPATH=src python scripts/build_native.py
PYTHONPATH=src python scripts/compile_datapass.py
PYTHONPATH=src:tests python -m unittest test_atlas test_native test_datapass_reader test_release_proof test_datapass_contract test_datapass_resale -v
PYTHONPATH=src python scripts/count_release_lines.py
PYTHONPATH=src python scripts/release_proof.py
PYTHONPATH=src python scripts/release_proof.py --verify
```

Collection/build scripts refuse this owner's Mac checkout. For a portable
self-hosted C++ build, `native/CMakeLists.txt` provides the library, tests and
benchmark targets. No service can use a native library without its absolute
operator-provisioned path and SHA-256 pin. Neither configuration enables trading.
The public manifest is not notarized; independently rerun the listed commands
and compare provider archives, contract bytecode and the published source commit.
