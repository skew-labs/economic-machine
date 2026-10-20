# Wallet connectors

The console supports browser wallets and a lazily loaded Privy wallet provider.
Both enter the same single-use, browser-bound SIWE challenge. Privy login alone
does not grant a SKEW workspace, spending policy or payment signature. An existing
wallet retains its workspace by address; a new embedded wallet has a new address
and a separate workspace. We do not merge accounts by email.

Set `MACHINE_PRIVY_APP_ID` on the portal and runtime. Optionally set
`MACHINE_PRIVY_CLIENT_ID`. These identifiers are public. No Privy app secret is
required for this client-owned wallet integration. Enable your intended login
methods and exact HTTPS origin in the Privy dashboard. The SDK build is pinned:

```sh
cd scripts/wallet-browser
npm ci --ignore-scripts
npm run build
```

Serve `web/privy` build outputs with the portal. Never expose source maps or
operator configuration. Only the console receives Privy's required CSP
exceptions. Payment/launch pages retain their existing strict policy. React and
Privy are loaded on demand; browser-extension login has no SDK dependency.

## MetaMask Agent Wallet

Install the official `@metamask/agent-wallet` CLI into the operator environment.
Use a dedicated OS user whose home is inaccessible to the web process. Complete
official CLI login and `mm init --wallet server-wallet --mode guard`. Check
`mm doctor` before wallet commands. The CLI wallet may differ from the browser
wallet, even for the same email. Never place a CLI/refresh token in application
JavaScript, a public environment variable, a subprocess argument or Git.

The observation bridge deliberately has no signing or command-execution API.
An operator supplies `MACHINE_METAMASK_OWNER_ADDRESS` (the console owner's proved
EOA) and `MACHINE_METAMASK_STATUS_FILE`. A private CLI observer publishes only
`authenticated`, `initialized`, `mode`, `address` and `observed_at`. The file must
be regular, at most 8 KiB, not group/world writable, and no older than 180 seconds.
The producer must use actual CLI readiness and selected-address reads; a
configured address is not authentication evidence. Failed reads replace the
snapshot with an unauthenticated status.

`scripts/metamask-agent-status.py` implements this observer. Run it under the
private CLI OS account. Give the web process read access only to its sanitized
output directory, never the CLI home. Example systemd units are in
`deploy/metamask-agent-observer.service.example` and the corresponding timer.
Configure executable paths and a writable status directory in the environment
file before enabling them. Tokens remain in the CLI's owner-only state. The
observer checks the CLI every minute; an unavailable/expired session is reported
as unavailable rather than synthesized from the last successful address.

Only that SIWE-authenticated owner may inspect or add the Agent Wallet. Agent
API keys cannot access the connector-management endpoints. Adding is idempotent
and installs the existing read-only Arbitrum account connector into that owner's
engine workspace. Use Sync to fetch balances. Shared budgets, reads and agent
account selection then use the regular Engine routes. Transaction signing stays
in the external wallet; this bridge does not enable automatic CLI spending.

API routes:

- `GET /wallet-config`: public Privy IDs only.
- `GET /api/wallet-connectors/metamask`: owner-specific readiness.
- `POST /api/wallet-connectors/metamask/connect`, body `{}`: add the proved CLI
  address to the owner's Engine accounts; cannot override address from a client.

No customer funds or wallet signatures are needed to run the tests. Live login,
read-only account discovery and an actual transfer are separate verification
steps. Do not describe a mounted login modal as a completed wallet login.
