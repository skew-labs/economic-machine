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
