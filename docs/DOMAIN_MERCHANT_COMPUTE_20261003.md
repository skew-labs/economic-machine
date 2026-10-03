# Domain, subscription merchant and compute connection — 2026-10-03

## Deployed behavior

- `skew.deals` stays on its existing owned Site. Root redirects to the new
  Skew landing at `/home/index.html`; the older `/app` routes, D1 binding and
  backend remain in the same deployment. Existing audience is preserved.
- Launch App opens the existing Economic Machine console. Its Buy services
  screen now includes a keyless inference-provider price check. Subscriptions
  initially identified the missing receiving-wallet configuration; the later
  owner-requested wallet setup is recorded below.
- The production runtime loads an explicit merchant registry through a systemd
  credential. The initial registry was `{}`. A newly generated owner-approved
  recipient is now configured for Atlas Monthly; historical demo wallets were
  not reused. The exact unsigned checkout is verified, with no customer payment.

## Subscription execution boundary

`Checkout → shared payment limit → exact x402 challenge → customer EIP-712
signature → facilitator verify → persisted single settle attempt → finalized
chain readback → terms-bound grant → one 30-day entitlement`

The merchant never has a wallet key. A lost HTTP response does not authorize a
second settlement call. Recovery requires a canonical finalized transfer and
authorization nonce, preserves held capital while unknown, and isolates failed
records. Delivered grants retain their approved plan hash, payer and duration.
Managed offers are immutable; refreshing catalog availability does not modify a
previously approved quote or extend any human login.

`scripts/configure_subscription.py` prepares the exact Circle USDC resource,
managed seller and PayAI facilitator from an owner-supplied **public address**.
It defaults to dry-run, preserves existing resources and rejects implicit changes
to an already configured recipient. No key generation, payment or workload occurs.

PayAI's public `/supported` endpoint was checked for Arbitrum x402 v2 exact
support. Sponsor credits and optional merchant credentials are subject to the
provider's limits. Code/configuration readiness is not evidence of a paid sale.

## Compute scope

The live unsigned Gate402 request returned an Arbitrum USDC requirement for
1,000 atoms (0.001 USDC), a five-minute timeout and an external recipient. This
proves an HTTP payment challenge was observed. It does not prove model execution,
GPU capacity, a GPU lease or delivered output. This provider's purchasing flag is
false until its paid request, settlement and delivery adapter is admitted.

Untrusted discovery examples can contain decimal JSON numbers; their raw validated
JSON bytes are hashed. Payment math uses only the validated integer atomic amount.
Neither floating-point discovery examples nor provider metadata become engine
economic state. Failed checks remain unavailable and cannot authorize a purchase.

## Evidence

Focused tests cover only the new merchant/connection/provisioning boundary and
two existing checkout integration paths. The corrected preflight fixture models
the production schema and operator settings. Initial failures are retained;
the evidence summary selects the latest result for each named test.

- `artifacts/atlas-release/merchant-compute-tests.log`
- `artifacts/atlas-release/subscription-preflight-tests.log`
- `artifacts/atlas-release/compute-provider-tests.log`
- `artifacts/atlas-release/merchant-compute-live.json`
- `artifacts/landing-20261003/provider-browser-check.json`
- `artifacts/landing-20261003/skew-deals-deployment.json`
- `artifacts/atlas-release/merchant-validation.json`

The browser checks cover the changed provider/subscription surfaces at 1440 and
390 pixels, their real navigation, image loading and horizontal overflow. The
release manifest binds source, screenshots and evidence by SHA-256.
Customer signatures, submitted payments and started GPU workloads were all zero.

## Publication

- Site source: `fccc149e646d182f094cbe9c4da78e90f9a52514`
- Sites version: 16
- Deployment: `appgdep_6ac0b600fba88191b6f1f6d52c32f27c` (succeeded)
- Custom domain: `skew.deals`, provider active, SSL active
- Console runtime: `/srv/skew/economic-machine-commerce-20261002`

The later [subscription receiver setup](SUBSCRIPTION_RECEIVER_20261003.md)
records the new public address, root-only encrypted custody, live availability,
unsigned challenge and cancellation. Its evidence supersedes the earlier
unconfigured subscription status. Still required for compute: a fully admitted
paid inference adapter and a separately configured/approved GPU lease provider.
