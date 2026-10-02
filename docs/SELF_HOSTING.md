# Run your own Economic Machine

Economic Machine is MIT-licensed software. The local console and deterministic kernel run in your
environment. Exchange, data and AI credentials stay in your process environment; the connector database
stores environment **names**, not values. Provider API reads do not go through the commerce payment network.
x402 is a separate option for purchasing external data or computing services.

## Linux setup

Use an owner-controlled Linux host or your remote development environment. This repository's maintainers
build and test on the authorized Canada host. No production DB, credentials or wallet files belong in Git.

```sh
git clone https://github.com/skew-labs/economic-machine.git
cd economic-machine
python3 -m venv .venv
.venv/bin/pip install -e .
umask 077
export ENGINE_ADMIN_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
.venv/bin/economic-machine serve --db runtime/engine.sqlite3 --port 8800
```

Open `http://127.0.0.1:8800/engine`. If the runtime is remote, use your SSH client's loopback tunnel.
The CLI binds to `127.0.0.1`; it does not expose an unauthenticated account server. Enter the owner token
in your local console. The browser retains it in memory only, clears the input and never saves it to
localStorage, a cookie or the source tree. Reloading the page requires unlocking again.

Keep the token inside your password manager or owner terminal. Do not send it in chat or screenshots.
The standalone owner token grants local administrative access; this release does not add public
multi-user tenant management to that token. The hosted commerce application's existing wallet login
and scoped API-key system remain separate.

## Connections

| Profile | What the engine actually does | User configuration |
| --- | --- | --- |
| Arbitrum Sepolia wallet | Chain ID check, finalized-height ETH and Circle test-USDC balances | Public address; no wallet key |
| Binance Spot | Signed **GET** account balances and open orders | `BINANCE_API_KEY` and `BINANCE_API_SECRET` environment references |
| JSON data | Bounded authenticated HTTPS GET, store content fingerprint/size | URL and environment reference |
| OpenAI-compatible AI API | Read-only `/models` catalog | Models URL and environment reference |

Create a connection through the console and press **Refresh** on its connection card. Creation alone
does not test credentials or make a network call. No reader places orders, withdraws funds, signs a wallet
transaction, executes model inference or authorizes an x402 purchase.

URLs use HTTPS on port 443, without embedded credentials or queries. DNS is pinned before dispatch;
private or mixed-address destinations and redirects are rejected. Binance query signatures are generated
by the read-only adapter. The configured provider must not need a localhost/private-network endpoint.
No proxy environment or automatic HTTP retry is used.

Wallet quantities retain asset decimals. Unpriced ETH and USDC are not added together as “total USD.”
Binance Spot and USD-M derivative readers are available. USD-M positions retain signed quantity,
entry/mark/liquidation prices, maintenance margin and P&L in the margin asset. Sequential REST reads
are not an atomic snapshot. A failed refresh preserves
the last snapshot, marks it degraded/stale and exposes a bounded error code. Disconnecting removes that
source from current balances while preserving its recorded history; it does not revoke the provider's key.

The AI reader checks model availability without inference. Usage is a separate idempotent ledger populated
by a user's adapter through `POST /api/engine/usage`; its cost is **reported usage, not a provider invoice**.
Data payloads are fingerprinted without storing arbitrary provider content or claiming data quality.

## Programs, state and limits

The versioned ISA, static compiler, state deltas, kernel and durable runtime are extracted from the
original Economic Machine. Their unchanged versioned receipts and 195 portable transition vectors are
rechecked in this repository. The extraction imports no TRON adapter, dataset collector or model weights.

Run the synthetic conformance example at its recorded time:

```sh
.venv/bin/economic-machine run --program cases/economic_program_demo.json \
  --state cases/economic_state_demo.json --at 2026-09-25T12:01:00+00:00
```

The result is `AWAITING_AUTHORIZATION`, not a trade. The example program is now expired for current-time
operations. These files are conformance fixtures, not a live portfolio or investment strategy.

For your runtime, install a freshly sourced typed state, then register a reviewed program with your own
owner, network, asset, capital/exposure/cost/loss limits, allowed venues and expiry:

```sh
.venv/bin/economic-machine install-state --db runtime/engine.sqlite3 --state owner-state.json
.venv/bin/economic-machine register --db runtime/engine.sqlite3 --program owner-program.json
```

The unified console's **Agents & limits → Validate & register** checks the typed JSON and registers it; **Evaluate** runs deterministic
checks against the installed state. State evidence is deliberately separate from connection snapshots:
reading a wallet does not invent quotes, price evidence, liabilities or an economic mandate. An adapter
must supply a typed `StateDelta` through `POST /api/engine/deltas` or the CLI. Dependencies determine which
programs are recomputed. Unchanged observations do not invoke an LLM.

Program revisions supersede unsent intents while preserving locked capital. Pausing or expiry does not
release an ambiguous submitted execution lock. Finality, post-state and exception verification are explicit
external interfaces. Binance Spot LIMIT and one-way USD-M reduce-only LIMIT now have an execution
adapter; a generic wallet signer and additional venue adapters remain unimplemented.
The original commerce runtime's proven x402 purchase is exposed independently in the public recorded view.
Do not report a kernel receipt or local lock as an exchange fill or a new chain transaction.

## Scheduled synchronization and venue execution

`Connections` configures each source and selects manual, 15s, 30s, 60s or 300s
sync. The server scheduler is always running while the runtime service is alive;
enabled jobs, next run, backoff and leases survive restart. A browser tab is not
the scheduler. Failed reads keep old observations stale, and disconnect disables
the job without discarding recovery evidence. No LLM inference runs in this loop.

`Execution` creates an immutable venue turnover policy, prepares a LIMIT order,
and binds owner approval to the exact plan hash. The plan expires after 60s.
The broker re-reads the instrument rules and account before transmission.
Unknown outcomes keep their full turnover reservation, block another order on
that connection, and query the original client order ID; no new-order retry is
performed. Terminal fills use actual cumulative quote/quantity evidence.
Turnover is gross trading volume, not wallet spending or an x402 payment budget.
Fees are explicitly marked unreconciled; they are never fabricated as zero.

Order transmission is disabled unless the user's own service starts with
`ENGINE_ALLOW_LIVE_TRADING=1`. Enabling this gate does not replace exact per-order
owner approval. A trading API key should have no withdrawal permission and use
venue-side IP restrictions where supported. The published Canada services keep
the gate disabled. No live order, customer signature or mainnet transfer was
performed in this release.

The hosted console uses existing wallet login and stable wallet-owned databases.
Its profile response provides `credential_namespace`, such as
`ENGINE_<OWNER_HASH_PREFIX>_`. References outside that namespace are rejected so
one customer cannot select server/operator credentials. Supply actual key values
privately in the runtime environment, never in connection JSON or public source.
The standalone loopback installation accepts the user's normal environment names
and owner token, which is kept only in the current tab. API keys with `engine:read`
or `engine:write` are opt-in; existing commerce read/order keys gain no engine or
approval authority. Agents can prepare plans; only the owner can approve or send.

## Local API

All `/api/*` endpoints require `Authorization: Bearer $ENGINE_ADMIN_TOKEN`. Browser origins must match
the loopback console origin; there is no cross-origin credential grant. Request bodies are streamed with
a 200 KB bound. The standalone app serves only its HTML, JS, CSS and licensed icon assets.

| Operation | Endpoint |
| --- | --- |
| Read unified view | `GET /api/engine/overview` |
| Configure / refresh / disconnect | `POST /api/engine/connections`, `…/{id}/sync`, `…/{id}/disconnect` |
| Import adapter usage | `POST /api/engine/usage` |
| Install state / apply a delta | `POST /api/engine/state`, `POST /api/engine/deltas` |
| Check / register a program | `POST /api/engine/programs/compile`, `POST /api/engine/programs` |
| Evaluate / pause / resume | `POST /api/engine/programs/{id}/{operation}` |
| Verify a historical receipt | `GET /api/engine/receipts/{hash}/verify` |
| Locally lock admitted capital | `POST /api/engine/receipts/{hash}/lock`; no signing or submission authority |

Export source and receipts for review. Do not publish runtime databases: they can contain private balances,
positions, costs and account identifiers even though they do not store provider credential values.
