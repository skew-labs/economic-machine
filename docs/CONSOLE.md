# Console and agent access

The console is the owner's interface to the same Engine used by agent APIs.
Owners connect accounts, set shared limits, issue scoped credentials and inspect
execution records. A conversation can propose work; it cannot grant itself
wallet authority or raise a budget.

## Identity and accounts

Hosted wallet login uses a single-use, browser-bound SIWE challenge. Each verified
wallet address owns its own workspace. Self-hosted installations use a local
owner token. Development test sessions are not production wallet identities.

Connections refer to credentials in the operator's private environment. Account
reads preserve asset units, source, observation time and failure state. A stale
snapshot is not silently replaced with a fabricated balance. Connections can be
synchronized, scheduled or disconnected from the same console.

## Scoped agent keys

Owners create agent keys through `POST /api/keys`. A key's secret is shown once;
the store retains its hash and display prefix. Keys are bound to their workspace,
scope, expiry and, where applicable, an existing policy or named agent.

| Permission | Boundary |
| --- | --- |
| `read` | Workspace reads; no owner administration |
| `demands:write`, `supplies:write` | Buyer requests and seller rules |
| `orders:write` | Orders under the key's bound policy |
| `payments:request` | Request payment under an existing limit; no signature |
| `engine:read`, `engine:write` | Engine routes subject to their owner-only gates |
| `agents:run` | Tasks for the key's bound agent |

Agent keys cannot issue credentials, approve owner-only actions or increase limits.
An invalid Authorization header never falls back to an owner's browser cookie.
Revocation blocks subsequent requests; it does not reverse external transactions.
See [agent control](AGENT_CONTROL.md) for shared envelopes and bound-key semantics.

## Capital and completion

Reservations serialize competing agents against their common policy. A repeated
request ID reuses the existing operation; changing its input is rejected. Policy
capacity and development test credits are not wallet balances.

Payment activity distinguishes preparation, approval, submission uncertainty,
canonical settlement, access activation and delivery. Unknown external outcomes
retain their reservation until reconciled. A transaction hash alone is not success.
See [payment and delivery](SERVICE_COMMERCE.md) and [recovery](TASK_PAYMENT_RECOVERY.md).

The recorded-example view is separate from authenticated accounts and remains
read-only. It does not confer a balance, credential, allowance or signing authority.

## Visual workspace

The assistant opens the existing DataPass, Fuel, Mining and Connections views.
Fuel quotes and approvals stay inside the same console. Selecting a mining stage
explains the protocol transition; it does not execute that transition. The search
worker shows the actual C++ candidate path, cost and verification result. A local
candidate is distinct from chain publication or a claimed reward.

The public SKEW panel uses `GET /market/skew` (under `/commerce` when hosted).
An operator may configure `MACHINE_SKEW_TOKEN_ADDRESS` and
`MACHINE_SKEW_POOL_ADDRESS` after verifying the official Arbitrum One deployment.
Both must be nonzero EVM addresses. The configured SKEW token must be the pool's
base token. There is no symbol search and no caller-supplied provider URL.

Spot price, 24-hour volume and provider-reported market cap come from the pinned
[DEX Screener pair](https://docs.dexscreener.com/api/reference). The line uses
[GeckoTerminal hourly closes](https://api.geckoterminal.com/docs/index.html)
from that pool, in USD. It is a single-pool reference, not a consolidated index
or an executable quote. Missing intervals remain gaps. Market cap is unavailable
when the provider does not report it; fully diluted valuation is not a substitute.
The panel reports when the source was checked, caches for 60 seconds and refreshes
when the tab becomes visible. It does not infer exchange freshness from that time.

The default official address comes from the public
[Arbitrum One deployment manifest](../contracts/deployments/arbitrum-one.json).
An explicit empty/invalid operator override disables that identity. A different
configured token never inherits the official deployment proof. Without a pool,
price, volume, market cap and history remain absent.

Total issued supply is read from `totalSupply()` at the same recent block through
two RPC hosts. Chain ID, block hash, runtime hash, decimals and cap must match.
The panel shows the observation block and the issued percentage of the cap.
These are latest-block observations, not finalized supply or circulating supply.
Failed or stale reads show unavailable, never zero or a stale cached number.
The independent supply cache lasts 60 seconds. RPC credentials remain server-side.
No wallet authentication is needed; this endpoint cannot sign, swap or mint.

## Public routing

`https://skew.deals/` serves the same landing source as the console portal.
`/home`, `/home/`, `/home/index.html` and the former commerce landing redirect
to `/`. Legacy product links under `/home/` redirect to their `/commerce/`
counterparts. `/commerce/console` is the sole console entry point.
The Sites worker must run before static assets (`assets.run_worker_first: true`)
so a historical static copy cannot bypass these redirects. Only allowlisted
commerce session cookies reach the fixed upstream; landing requests do not
forward credentials. Old application APIs remain inaccessible.
