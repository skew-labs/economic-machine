# Solana v6: deployed development engine

The program uses a fixed account arena and explicit integer accounting. The
`wide256` feature supports 256 seats, 4,096 resting orders and a 549,440-byte market.
The tested devnet program is `ChfNY1go7qaJYPK3EJvPZwX99yjEBdnaYzLMgUoiPEa6`.
Its recorded ELF SHA-256 is
`6bf2bc652ff4dbe232f1b77021a5ecd40e4e237fd9dac99a28b25aea401497be`.
Test collateral is a six-decimal SPL token, not real USDC.

| Directory | Purpose |
| --- | --- |
| `program` | Matching, isolated margin, funding, oracle/session checks and resolution |
| `client` | Wire encoding and development RPC fixture helpers |
| `agents` | Native adapters, shared observations, exact-byte transport and journals |
| `muse` | Policy contract, data projection and bounded response validation |
| `evolution` | Typed DAG admission, reference evaluation and generation archive |
| `research` | C++ evaluation, immutable profile revisions and durable history |
| `adaptive` | Scoped flat-state handoff adapter; no pilot grant is distributed |
| `tests` | Offline SBF and native-boundary checks |

## Build on Linux

Requirements: C++20, SQLite/OpenSSL development libraries, Solana `cargo-build-sbf`
with platform tools 1.57, and Python with solders, aiohttp and zstandard.

```sh
cd markets/solana/v6
bash build-native.sh
bash build-research.sh
bash build-sbf.sh wide256
export MP_LAYOUT=wide256
export MP_ELF="$PWD/build/sbf/machine_perps.so"
export PYTHONPATH="$PWD/tests:$PWD/agents:$PWD/client:$PWD/muse:$PWD/evolution:$PWD/research"
python ../../run-solana-tests.py --version v6
python -m unittest discover -s evolution -p 'test_*.py'
python -m unittest discover -s research -p 'test_*.py'
```

Run `compact16` in a separate output directory if needed; do not load compact
account bytes under the wide ABI. SBF tests use LiteSVM and synthetic tokens.
They do not submit transactions. `muse/controller.py` is an explicit provider
adapter, not part of these tests and not a source of signing authority.

`MP_BASE` selects an operator-owned development namespace. The public default is
the version's `runtime` folder, with no keys or deployment files. A live controller
must provide its own immutable mandate, existing session scope and journal. Do
not recreate a journal to recover an unknown outcome.

The six normalized strategy fixtures preserve the numerical DAG bodies used in
the experiment. They contain no provider responses, account addresses, private
user profiles or API credentials. Their use reproduces native conformance tests,
not historical network execution or independent profitability.
