# Services, x402 checkout and Atlas subscriptions

The console now exposes **Buy services**, **Subscriptions**, **Data licenses**,
**Service payments** and **API keys** inside the same Engine workspace.

## Customer flow

1. Sign in with the wallet that will pay.
2. In Buy services, choose an active seller offer, quantity, usage rights and maximum total.
3. Review the negotiated total, immutable version, network and recipient.
4. Select a shared payment limit, or create one directly in checkout.
5. Prepare the payment and validate the merchant's x402 challenge.
6. Approve the exact EIP-3009 authorization in the wallet. The console never holds a wallet private key.
7. Follow the same purchase record through chain reconciliation and delivery.

An expired seller rule is unavailable. A registered endpoint alone is not an
available product. A compute listing does not prove physical GPU capacity or
authorize starting a GPU workload. Local exchange orders do not pass through
commerce payments. DataPass contract licenses are a separate purchase path;
an NFT license must not also be charged by x402.

## Native DataPass purchase

Data licenses has an explicit two-step buyer workflow: review the current
version-bound plan, approve only the exact test-USDC amount, then buy the
license in the connected Arbitrum Sepolia wallet. The browser reconstructs
both transaction payloads and checks the token, contract, buyer, chain,
price, report/terms roots and purchase ID before requesting each transaction.
The purchase step requires the token allowance to be visible first.

`GET /commerce/api/data/purchase-status?purchase_id=0x…&version=…` reads the
buyer's ID through both pinned finalized RPC providers and delivers only after
verifying the resulting owned, unexpired license. An absent finalized purchase
is `NOT_FINALIZED`; it never permits a blind payment retry. Public unsigned
plans and transaction IDs survive a tab reload in wallet-scoped session storage.
Wallet rejection can be retried; an uncertain submission disables another
signature and retains the purchase ID for reconciliation. This is separate
from x402 and Atlas Monthly. Deployment, release administration and resale
produce unsigned plans and require separate operator approval.

## Agent keys

The key dialog has three presets: account/data reader, buyer, and seller.
The buyer preset uses `read`, `data:read`, `demands:write`, `payments:request`.
Payment permission binds to one owned mandate; every linked key shares its
budget. Seller keys use `read`, `supplies:write`; they cannot pay, issue keys,
raise limits, or administer another workspace. Secrets appear once and are
stored hashed. Neither preset acquires wallet signing authority.

Use `Authorization: Bearer $MACHINE_API_KEY` in your agent environment.
Never embed the key in a URL or commit it to source.

| Operation | Endpoint | Permission |
| --- | --- | --- |
| Public current catalog | `GET /commerce/api/commerce/catalog` | Public |
| Register buyer policy | `POST /commerce/api/demands` | `demands:write` |
| Register seller policy | `POST /commerce/api/supplies` | `supplies:write` |
| Refresh seller data | `POST /commerce/api/supplies/{id}/refresh` | `supplies:write` |
| Read buyer matches | `GET /commerce/api/demands/{id}/matches` | `read` |
| Quote a purchase | `POST /commerce/api/commerce/checkouts` | `demands:write` |
| Prepare its x402 payment | `POST /commerce/api/commerce/checkouts/{id}/prepare` | Bound `payments:request` |
| Validate challenge | `POST /commerce/api/payments/{id}/challenge` | Bound `payments:request` |
| Submit external signature | `POST /commerce/api/payments/{id}/submit` | Bound `payments:request` |
| Reconcile ambiguous payment | `POST /commerce/api/payments/{id}/reconcile` | Bound `payments:request` |
| Read purchase + delivery | `GET /commerce/api/commerce/checkouts/{id}` | `read` |
| Read subscribed Atlas data | `GET /commerce/api/commerce/subscriptions/atlas-monthly/delivery` | `data:read` + active paid period |

Checkout request:

```json
{
  "resource_id": "atlas-monthly",
  "supply_id": "supply-id-from-current-catalog",
  "plan_id": "atlas-monthly",
  "units": 1,
  "purpose": "research",
  "license": "internal-use",
  "max_total": "10",
  "max_age_seconds": 86400,
  "max_refresh_seconds": 60,
  "response_seconds": 30,
  "idempotency_key": "unique-buyer-purchase-reference"
}
```

Prepare with `{"mandate_id":"your-existing-approved-limit"}`. Preparation
creates one durable capital reservation. Repeating the same checkout cannot
create a second payment. Changes to terms or the payment registry require a
fresh reviewed purchase. After transmission uncertainty, read/reconcile the
existing payment; never automatically send another authorization.

## Atlas Monthly: 10 USDC

Owner-approved price: **10 USDC for a 30-day prepaid access period**. This is a
fixed 30-day period, not calendar-month automatic billing. Engine Community
remains free, self-hosted, and MIT licensed.

The default plan publishes the price even before a merchant is connected.
It reports `PAYMENT_PROVIDER_NOT_CONFIGURED`, and cannot prepare a payment,
until the operator registers its payment resource and a current seller offer.

`MACHINE_SUBSCRIPTIONS_FILE` can override the bounded plan registry:

```json
{
  "atlas-monthly": {
    "name": "Atlas Monthly",
    "description": "A prepaid month of Atlas data access for your agents.",
    "resource_id": "atlas-monthly",
    "duration_seconds": 2592000,
    "price": "10"
  }
}
```

The approved x402 resource must use `data_type: subscription.atlas-monthly`,
a version identifying the subscription SKU, the actual seller's workspace,
an approved HTTPS merchant and finalized RPC, and Circle USDC on Arbitrum One
or Arbitrum Sepolia. Recipient and merchant are operator configuration, never
inferred from an old demo wallet. The current hosted registry has only an
Arbitrum Sepolia block-data merchant; it is not the Atlas subscription merchant.

The seller must publish matching purpose/license/asset/version rules with
`unit_price` and `floor_price` equal to `10`, no discount, and one unit per
subscription period. A different negotiated amount is rejected.

The merchant's terms/version-bound delivery must contain:

```json
{
  "subscription_grant": {
    "plan_id": "atlas-monthly",
    "plan_hash": "canonical-plan-hash-from-catalog",
    "payer": "approved-payer-address-in-lowercase",
    "duration_seconds": 2592000
  }
}
```

This object goes inside the normal resource delivery's `data` field. The outer
delivery includes the exact `terms_hash` and `data_version`. Only `SETTLED`
plus finalized `PAID` observation and the exact grant activate access. Merely
opening the purchase dialog, signing in, receiving a transaction hash, or
receiving an unrelated data object cannot activate a subscription. Missing
grant delivery retains the payment receipt and reports an access error.

Activation persists once per checkout. Repeated reads or restart cannot extend
expiry. A manually purchased renewal adds its period after existing paid access,
so early renewal does not discard remaining days. Expired access fails closed.
There is no auto-debit, recurring wallet allowance, or renewal worker.

The subscribed data endpoint serves the current verified original Atlas derived
report. Public list-price research is not executable GPU inventory. Subscription
access does not turn upstream public prices into a capacity guarantee or a
redistribution license for raw provider archives.

## Compute seller connection

An approved provider uses a machine-readable SKU such as `compute.gpu-hour`
with its registered version, machine delivery endpoint and actual seller owner.
Buyer/seller rules specify units, price caps, refresh time, response deadline and
usage rights; checkout reuses exactly the same market and x402 path.

A production compute adapter must separately implement capacity admission,
reservation/lease identity, job launch, delivery evidence, and expiry/recovery.
This checkout does not invent a compute seller or bypass the applicable signed
lease required to start work. Only registered, admitted merchants may enter the payable catalog.

The hosted seller implementation is now available in `machine_commerce.merchant`.
It forwards only an admitted customer's exact authorization to the configured
facilitator, commits one settlement attempt before transmission, and recovers
ambiguous outcomes through finalized token/nonce/transfer observation. There is
no signer in the subscription service. The facilitator is PayAI's exact-EVM
x402 v2 endpoint; its public supported-methods endpoint includes Arbitrum One.

An owner-approved **public receiving address** is required before enabling the SKU.
On the trusted host, run `scripts/configure_subscription.py` with `--recipient`,
`--resources`, and `--merchants`. Dry-run is the default; `--apply` writes the
registries without signing or transferring funds. Load both files in the same
service revision using `deploy/subscriptions.conf`. Existing differing recipients
are rejected rather than silently replaced. The managed seller identity publishes
immutable 10-USDC offers and refreshes them without extending human logins or
changing an in-flight quote. Configure the receiving address separately for each installation.
Source code does not provision custody or authorize receipt of customer funds. Receiving keys
must remain outside the API service.

The console also lists a keyless Gate402 inference connection. Its unsigned
Arbitrum USDC challenge is queried through a bounded, cached, DNS-pinned request.
The observed numeric amount is not a GPU capacity or delivery guarantee. This
provider is **outside the payable catalog** until an admitted request/delivery/
recovery adapter exists; `purchase_enabled` remains false. A read-only connection
check requires an authenticated owner and sends no wallet signature. Existing
GPU lease and workload approval requirements continue to apply.

## Integration tests

Tests cover checkout replay/concurrency, budget rollback, expiry, seller and SKU
binding, mandate scopes, subscription grants, finality, access and renewal. An
isolated EVM test connects an EIP-712 authorization, token transfer, checkout and
delivery. Local EVM results do not establish public-chain settlement.

Protocol references: [x402 EVM exact specification](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md)
and [Circle USDC addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses).
