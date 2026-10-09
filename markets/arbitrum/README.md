# Arbitrum perpetual orderbook and native coordinator

Solidity 0.8.30 implements packed quote/IOC commands, price-time matching, bounded
visits, isolated margin, cumulative IOC fees, funding and a global solvency index.
The C++ adapter uses the pinned Economic Machine runtime and emits compact
calldata; Python handles observation, durable reservations and reconciliation.

The bounded Arbitrum One experiment used
`0x47c28de5a38d434d9da58e5e636138a4fdf6a20b` and six controlled agents.
The session ended paused with account cash, positions, orders, delegated sessions,
allowance and bad debt at zero. The research companion distinguishes this pilot
from Anvil gas benchmarks and independent market trading.

## Offline verification

Requirements: Linux, C++20, Foundry, and Python packages in
`agent/requirements-lock.txt`.

```sh
cd markets/arbitrum
forge test -j 2
bash build-native.sh
python -m unittest discover -s agent -p 'test_*.py'
```

Foundry tests use synthetic collateral/oracle fixtures. Historical attack findings
are described in the research paper; private baseline snapshots are not dependencies
of this version. Fork tests in `fork` are optional read-only upstream-state tests.
The standard suite needs no RPC credential, signer or real funds.

`agent/service.py` checks an operator-supplied immutable mandate by default.
Its `--run` mode requires a separately authorized external signer socket.
The sender, chain, deployed runtime hash, profile revisions, initial nonce,
deadline and aggregate gas reservation remain bound across restarts. It does not
open accounts, deposit, withdraw or raise limits. Private pilot controllers and
owner-key launch commands are not part of this publication.

The market halts withdrawal under latent global insolvency. Large shock tests
required separately measured external recapitalization. No autonomous ADL,
backstop capital or loss-free strategy is claimed.
