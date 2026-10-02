# Economic Machine Commerce

A shared execution layer for buying and selling agents: typed trading policies, event-based discovery,
bounded negotiation, capital admission and noncustodial x402 execution. Independent of the TRON allocation
project and GPU/RAM marketplace. The platform does not certify or resell all seller data.

Agents use the API. People use the English API keys / Funds & limits / Activity console.
LLMs can draft policies or resolve exceptions; routine matching, negotiation, expiry and payment recovery use code.

## Implemented

- Typed buyer/seller conditions: asset, price, quantity, freshness, cadence, purpose, license and response deadline.
- Indexed event matching; demand registration returns agreements atomically in one HTTP response.
- Seller offer → bounded discount/floor → buyer acceptance or explicit escalation.
- Version and expiry invalidation; 100 active policies per side/data type limits matching fan-out.
- Hashed scoped API keys with revoke/expiry, shared test policies and real payment mandate binding.
- x402 v2 exact EVM EIP-3009 adapter: registered resources, challenge admission, external customer signature,
  at-most-once transmission, persistent capital holds, independent nonce/receipt/transfer reconciliation.
- Paid-but-missing-delivery and expired-unpaid states; no blind retries or fabricated refunds.
- Production configuration gate, provisioned operator login, secure cookies, durable rate limits,
  streaming request limits, bounded DNS-pinned HTTPS egress and sandbox spending disabled in production.
- Remote benchmark against both full-scan direct clients and cached direct clients, with identical trade policies.
- Actual Qwen3-32B comparison with bounded repair, measured provider tokens, initial policy cost,
  paid test-token outcomes and independently recalculated evidence.
- Existing CSV/RPC service examples and separate ERC-20 escrow: test ledger/PyEVM, not public deployments.

## Verification and product result

All compute runs on the owner-selected Canada server at `/srv/skew/economic-machine-commerce-20261002`.
The Mac is used only for editing, small reads, browser review and remote orchestration.

The original all-code comparison uses 64 suppliers and 24 paired trade requests over real localhost HTTP.
It measures requests and agreement latency, not real customer purchases or WAN performance.

| Method | Trade requests | Agreement p50 / p95 | Valid agreements |
| --- | ---: | ---: | ---: |
| Direct full scan | 1,560 | 417.1 / 450.2 ms | 18/24 |
| Cached direct client | 192 | 67.3 / 76.3 ms | 18/24 |
| Economic Machine | 24 | 54.0 / 60.6 ms | 18/24 |

The engine uses 87.5% fewer trade requests than the cached client, with a paired median latency ratio 0.801
(bootstrap 95% interval 0.755–0.862). All three paths use zero LLM tokens: token savings and higher agreement
rate were **not demonstrated by that all-code experiment**. All 18 feasible trades succeed and all six infeasible trades are rejected.
Initial seller registration costs 65 requests/3.16 s; including setup, the engine takes 4.43 s versus 1.66 s
for the cached direct client over this first 24-trade workload. Warm-path gains are not a cold-start victory.
See [comparison methodology](docs/COMPARISON.md) and [raw evidence](artifacts/comparison.json).

The real-model follow-up uses the live Kiln Qwen3-32B API. In its fixed fast-mode workload, the per-event
Qwen loop completes 10/24 feasible trades versus 24/24 for the engine, with verified x402 test-token
payment/delivery. Tokens including initial policy interpretation fall from 122,242 to 417: **99.66% less**.
The cached direct code client also completes 24/24 with 417 startup tokens; this is an advantage over the
measured model loop, not over every possible direct agent. See [live-model methodology and boundaries](docs/QWEN_COMPARISON.md).

The additional thinking-mode study uses 16 distinct requests: under a predeclared five-second agreement
budget the engine completes 12/12 feasible orders versus 0/12; Qwen produces six compatible choices late.
Startup-inclusive token reduction is 99.13%. Both completed studies reconcile 82 actual ephemeral test-token
payments. These controlled outcomes do not establish real-customer conversion or public-chain performance.

The prior runtime release has 87 passing cases and eight unchanged historical escrow cases. This follow-up
adds 17 targeted tests and independently checks both studies; it does not rerun the unchanged core suite.
The existing compiled EIP-3009 test token is reused. [Verification](docs/VERIFICATION.md) records the scope.

## Live status

The private server is running **development mode**. The x402 runtime is implemented and tested with actual
EIP-712 signatures and token transfers in ephemeral PyEVM. The merchant/facilitator is a test fixture.
A real Arbitrum RPC read through the new pinned TLS transport succeeds; that is not a real payment.
No public merchant registry, customer signature, public-chain payment, public production deployment or
submission is asserted. Production preflight currently rejects this unconfigured development environment.

To activate public capital admission, configure a real HTTPS origin, operator credentials and approved
merchant/token/recipient registry, then complete the external operation gates in [Production](docs/PRODUCTION.md).
Secrets and signing keys stay outside this repository and chat.

## Agent API

| Endpoint | Responsibility |
| --- | --- |
| `POST /api/sessions` | Development session, or production operator login |
| `GET/POST /api/keys`, `POST /api/keys/{id}/revoke` | Owner credential management |
| `POST /api/demands` | Register buying conditions and return matching agreements |
| `GET /api/demands/{id}/matches` | Read an owned demand's current agreements |
| `POST /api/supplies`, `POST /api/supplies/{id}/refresh` | Register selling policy / version event |
| `GET /api/market` | Read policies, agreements and escalations |
| `POST /api/payment-mandates` | Owner sets external payer and shared budget |
| `GET/POST /api/payments` | Read records / atomically prepare one agreed payment |
| `POST /api/payments/{id}/challenge` | Admit merchant request and return external signing message |
| `POST /api/payments/{id}/submit` | Accept customer signature and transmit at most once |
| `POST /api/payments/{id}/reconcile` | Verify chain outcome; no transmission |
| `POST /api/payments/{id}/cancel` | Cancel a never-transmitted request only |

Legacy `/api/matches/{id}/payment-request` remains blocked; real execution uses the explicit mandate/payment
API. Test-credit policies and orders remain development examples only. Browser owner access uses HttpOnly
cookies; agents use scoped bearer keys. API keys cannot create payment signatures or raise their limits.

## Remote-only commands

```sh
# Run on the authorized remote host, never the Mac.
.venv/bin/pip install -r requirements-verified.txt
.venv/bin/pip install -e . --no-deps
.venv/bin/python scripts/compare_routes.py
.venv/bin/python scripts/read_chain.py
.venv/bin/python scripts/production_preflight.py --output artifacts/production-preflight.json
```

`requirements-verified.txt` pins the tested remote environment including verification dependencies; it is not
a hash-locked supply-chain attestation. The private systemd service listens only on localhost:4260 and uses
`/var/lib/machine-commerce/commerce.sqlite3`. Public TLS/configuration examples are provided separately.

[Architecture](docs/ARCHITECTURE.md) · [Production contract](docs/PRODUCTION.md) ·
[Verification](docs/VERIFICATION.md) · [Original extraction](docs/EXTRACTION.md).
