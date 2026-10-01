# Current console and access contract

The English console manages API keys, Funds & limits and Activity. Discovery/negotiation run through agent APIs.
Production adds provisioned operator sign-in; external payment limits bind one payer, token, resource allowlist,
budget and lifetime to payment-enabled keys. Creating a limit grants no signing authority and moves no funds.
The console does not present mandate capacity as a wallet balance. Real signatures remain customer-owned.

Payment records distinguish prepared, externally signed submission, reported settlement, independently
confirmed payment, missing delivery and expired unpaid outcomes. Test credits remain visible only in development.
No approved real merchant is configured in the active private workspace; payment-limit creation is disabled there.
A payment-enabled key cannot be created in this UI without an active limit. Production owner and agent credentials
cannot come from old anonymous development sessions. Read, demand, supply, test order and real payment scopes
remain separate. Agents cannot issue credentials or increase limits.

Detailed endpoints and external signing sequence: [Production](PRODUCTION.md). The notes below describe the
original development-console release and should not be treated as public production verification.

---

# Console and agent access

Agents consume the Economic Machine API. People use the console to grant access,
set spending limits and inspect outcomes. Discovery and negotiation are API operations.
The console is deliberately limited to **API keys**, **Funds & limits** and **Activity**.

## Credentials

The existing HttpOnly owner cookie manages a 24-hour test workspace. This is a local
test identity, not production customer authentication. The compatibility session token
still carries owner authority; do not distribute it as a restricted agent credential.

An owner creates an agent key with `POST /api/keys`:

```json
{
  "name": "Research agent",
  "scopes": ["read", "orders:write"],
  "policy_id": "YOUR_OWNED_POLICY_ID",
  "ttl_seconds": 3600
}
```

The response contains public metadata and a secret beginning `em_test_`. The secret
appears only in this creation response. SQLite stores its SHA-256 hash and a display
prefix. Lists, journals and verification evidence do not contain the full secret.
The browser does not place it in local storage; closing the one-time dialog clears its field.

| Permission | Agent operations |
| --- | --- |
| `read` | Workspace balances and policies, market snapshot, owned orders, artifacts and receipts |
| `demands:write` | Register buyer demand |
| `supplies:write` | Register seller rules and refresh data versions |
| `orders:write` | Reserve, run and cancel orders under the bound policy |
| `payments:request` | Recheck agreed terms at the payment gate; live payments remain blocked |

Keys cannot create policies, issue or list keys, revoke credentials, or gain owner
authority. Unknown authenticated mutations default to owner-only. A supplied invalid
authorization header never falls back to an owner's browser cookie.
The `read` permission is workspace-wide; it is not an order-by-order confidentiality boundary.

Lifetime is 60–86,400 seconds, bounded by the owner session and, for order keys,
the linked policy expiry. A workspace can have up to 100 active keys. Revoking is
idempotent and rejects subsequent requests. Requests admitted before revocation,
existing orders and settlements are not retroactively cancelled.

## Capital

An order key must bind an active policy belonging to its owner. It cannot select another
policy or execute/cancel an order on another policy. The existing transactional ledger
enforces allowed services, the per-order cap, the shared total budget and account balance.
Two keys on one policy consume the same cap. Idempotency does not create another reservation.

Policies do not move funds. Order admission reserves credits; verified delivery settles
them; failed or expired delivery refunds the reservation. The funds page separates
available credit, held reservations and settled spending.

This service currently uses **TEST_CREDIT** with no monetary value. A new test workspace
starts with 10 credits. There is no wallet connection, token deposit, withdrawal,
customer signing, facilitator settlement or chain deployment behind this console.
Market agreements stay distinct from the example service ledger.

## Evidence

All compute checks run on the owner-selected Canada server in
`/srv/skew/economic-machine-commerce-20261002`.

- `access-tests-final.log`: 14 credential/authority cases.
- `console-api-tests-final.log`: 6 checks of changed API connections and existing callers.
- `console-agent.json`: actual HTTP key creation, scoped CSV order, delivered output,
  test-ledger receipt, rejected over-budget order and revoked-key HTTP 401. The key is
  revoked at the end; secrets are omitted from evidence.
- `console-browser.json`: English UI, connected balances, key setup dialog, order details,
  receipt download and 390px responsive checks. Browser testing did not submit a key creation.

Previous core, EVM and x402 evidence is retained. Those unchanged checks were not repeated.
This evidence does not establish production authentication, load capacity or live payments.
