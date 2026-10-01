# Production operation and payment contract

This release implements a bounded, single-host x402 buyer execution service. It is not a public launch,
a security audit, a data marketplace with guaranteed stock, or a claim of hedge-fund operational readiness.
The active Canada-host service remains a private development workspace until real operator credentials,
an owner-controlled HTTPS origin and approved external resources are configured. No customer payment was sent.

## Runtime and capital authority

Agents register typed demand/supply policies and receive matching agreements in the demand registration
response. No second discovery request is required. Refresh and expiry invalidate old agreements.
Each data type admits at most 100 active buyer policies and 100 active seller policies; rejection is atomic.
This bounds event fan-out and guarantees the first 100 agreements are not a silently truncated allocation.
Quantity limits are per order, not a seller inventory reservation system.

An owner creates a payment mandate: external payer, explicit CAIP-19 token, total budget, per-payment limit,
resource allowlist and lifetime. Payment-enabled API keys bind to exactly one mandate. Multiple keys share
its reserved and confirmed amounts. Agents cannot create keys, change mandates or increase capital limits.
Mandates govern this runtime only. They are not a deposit, a wallet-wide spending lock or proof of wallet ownership.
Separate mandates may refer to the same wallet; token balance is checked when admitting a challenge.

The platform has no private key, signer, facilitator gas wallet or automated signature authority.
The customer or an independently controlled customer agent signs the precise EIP-712 message externally.
An API key alone cannot produce an EIP-3009 authorization. EIP-3009 binds payer/recipient/token/amount/window/nonce;
it does **not** cryptographically bind data quality, the negotiated license or our terms hash.

## Supported x402 adapter

Only x402 v2 `exact` EVM EIP-3009, HTTPS POST, registered structured JSON resources and six-decimal tokens.
Permit2, EIP-1271 smart-wallet signatures, EIP-6492, fee-on-transfer/rebasing tokens and arbitrary GET resources
are not supported. The adapter fails closed on these cases instead of silently changing payment methods.
Each approved resource must match the registered seller owner, data type and data version. After a version
change, update the operator registry deliberately and restart; pending payments with a changed profile stop admission.

The unsigned POST sends terms_hash, data_version, units, purpose and license. It must return HTTP 402 with
`PAYMENT-REQUIRED`. After approval the same POST carries `PAYMENT-SIGNATURE` and an idempotency key.
The resource handles its own facilitator. Our platform does not choose or store a facilitator signing key.
After settlement the response must include `PAYMENT-RESPONSE` plus JSON:

```json
{"terms_hash":"<agreed hash>","data_version":"<registered version>","data":{}}
```

Terms/version binding is checked; data truth, legal ownership and license enforcement are not certified.
Paid missing/invalid delivery is recorded for dispute, never labelled a refund. x402 payment and delivery are
not an atomic exchange. The separate escrow contract is not deployed or automatically used by this adapter.

Protocol references: [x402 v2 specification](https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md),
[HTTP transport](https://github.com/x402-foundation/x402/blob/main/specs/transports-v2/http.md),
[exact EVM scheme](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md),
[EIP-3009](https://eips.ethereum.org/EIPS/eip-3009).

## Agent payment sequence

1. Owner signs in; creates a payment mandate and a scoped key bound to it.
2. Agent posts a demand with `payment_asset: "eip155:42161/erc20:<approved-token>"`.
3. Agent selects one returned agreement and posts `/api/payments` with match_id, terms_hash,
   mandate_id, resource_id and idempotency_key. This atomically reserves mandate capacity.
4. POST `/api/payments/{id}/challenge` admits the registered merchant challenge, verifies chain identity,
   token contract/decimals/EIP-712 domain and payer balance, and returns typed_data plus payment_template.
5. Customer signs externally. Insert that signature into payment_template.payload.signature;
   base64-encode JSON and POST `{ "payment_signature": "<header>" }` to `/api/payments/{id}/submit`.
6. Read the result or POST `/api/payments/{id}/reconcile`. Never construct a second payment to recover a timeout.

`PREPARED → CHALLENGE_READY → SUBMITTED → SETTLEMENT_REPORTED/UNKNOWN → SETTLED`.
Terminal alternatives: `PAID_DELIVERY_MISSING`, `EXPIRED_UNPAID`, `CANCELLED` (never transmitted).
Capital remains held during ambiguity. An exclusive database transaction commits SUBMITTED before transmission.
Replaying submit returns the persisted record; it never retransmits the authorization. Signature plaintext is not stored. A registry error holds that record for review without stopping other pending records; failed RPC reads degrade worker health.

A crash immediately after that commit can lose a transmission. Recovery deliberately prefers a temporary hold
to a possible duplicate payment. The worker performs bounded batches with 15 seconds between batches; it never signs or sends payments.
A canonical successful receipt must contain both the exact AuthorizationUsed(payer, nonce) and exact
Transfer(payer, recipient, amount) from the approved token. Merchant tx hashes are claims until this check passes.
Unused nonce at a finalized block after expiration releases an unpaid hold. Missing RPC data does not.
Nonce searches cover at most 10,000 blocks; outside that range, an operator must provide transaction evidence
through an operator-reviewed recovery change. The current API intentionally offers no arbitrary tx injection.

Finality relies on the approved RPC's `finalized` tag and canonical block readback. No independent L1 proof is implemented.
[Arbitrum's public RPC documentation](https://docs.arbitrum.io/arbitrum-essentials/reference/node-providers)
provides the endpoints; public RPCs have no availability SLA. Use an approved reliable provider for real operation.

## Trusted-host provisioning and launch gate

Run only on the authorized remote host. Keep credential files outside the checkout, mode 0600, directory 0700.

```sh
.venv/bin/python scripts/provision_operator.py --file /etc/machine-commerce/operators.json --username owner
```

The CLI prompts without echo and atomically writes an scrypt hash. It does not print a password or issue an API key.
Create `/etc/machine-commerce/resources.json` as a map from resource ID to:

```json
{
  "approved-resource": {
    "url": "https://<approved-seller>/data",
    "rpc_url": "https://<approved-rpc>/rpc",
    "network": "eip155:42161",
    "asset": "<actual six-decimal EIP-3009 token address>",
    "pay_to": "<approved recipient address>",
    "token_name": "<on-chain EIP-712 name>",
    "token_version": "<on-chain EIP-712 version>",
    "max_timeout_seconds": 60,
    "seller_owner": "<provisioned seller workspace ID>",
    "data_type": "<typed resource category>",
    "data_version": "<current registered version>",
    "finality": "finalized"
  }
}
```

Placeholders are intentionally rejected. Verify the seller account and supply registration before allowing buyers.
Use `deploy/production.conf.example` as a reviewed systemd drop-in and `deploy/nginx.conf.example` for the
owner-controlled TLS proxy. Forwarded headers are trusted only from localhost; port 4260 must remain private.
Production mode requires provisioned passwords/resources/HTTPS, rejects anonymous workspace creation and
test-credit policy/order creation, and uses secure HttpOnly SameSite cookies. No social signup or OIDC is implemented.
Development credentials are rejected in production, and removed operators lose access even with old keys.
Peer and credential rate buckets persist across restarts; login additionally caps at five attempts per IP/minute.
Streaming mutations stop at 50 kB. Egress allows exact registered URLs only, rejects mixed/private DNS, pins a
public IP while preserving TLS hostname validation, has no redirects/retries, and bounds responses at 200 kB.

With the production environment and COMMERCE_DB configured:

```sh
.venv/bin/python scripts/production_preflight.py --output artifacts/production-preflight.json
```

This checks actual chain/token/domain interface, active seller, configuration and journal. Exit zero does not mean
the merchant facilitator, customer signing, delivery, backup restore or incident response have been proven.
Before public capital admission also require a reviewed merchant integration, a small separately approved payment,
encrypted off-host backup and a restore rehearsal, alerting on stale reconciliation/degraded health, a retention policy,
independent security review and an operator dispute workflow. Those external operational gates are still open.

## Storage and recovery constraints

One application process, SQLite WAL and BEGIN IMMEDIATE transactions; do not increase worker count to claim scale.
Journal verification happens once per exclusive transaction before appends, never cached across transactions.
This preserves corruption rejection without repeatedly scanning the same history within one matching event.
The journal has no external trust anchor and does not resist an administrator rewriting the whole database.
History grows with use; market sharding, external anchoring, organization roles, inventory reservations, push delivery,
multi-region failover and long-running capacity tests remain future work. No public availability guarantee is asserted.
