# Atlas subscription receiver — 2026-10-03

The owner explicitly requested creation of a new receiving address. The Canada
host created `0xD432a628a9860A8d0Be98c1782B2a0cD136Da00e` and configured
`atlas-monthly` to receive exactly 10 Circle USDC on Arbitrum One (`42161`) for
30 days. Billing is prepaid, with separate customer approval for each renewal.

## Key custody

`scripts/provision_subscription_wallet.py` runs only as root from `/srv/skew/`.
It uses OS entropy and an encrypted Ethereum V3 scrypt keystore. The encrypted
keystore and a separate randomly generated passphrase are stored as root-owned
0600 files in `/var/lib/skew-treasury/subscription-receiver` (0700). The directory
is outside source control and outside the API's systemd credentials. Only public
metadata is exported to release evidence. No private key, passphrase, signature,
or keystore contents belong in GitHub, browser storage or public evidence.

Provisioning is idempotent. Existing complete wallets are decrypted in memory to
check address consistency and then reused. Partial, permissive, linked, weak-KDF
or inconsistent vaults are refused; they are never silently replaced. Publication
uses a lock, a staged directory, fsync and rename. The API's actual DynamicUser
was separately tested and cannot open either secret file.

Both secret files are on the same trusted server. This is not an off-host backup,
hardware wallet or protection from compromise of server root. The merchant has
no signing access. Future treasury withdrawals require a separate owner action;
creating this receiver did not authorize or perform any withdrawal.

## Live admission and unsigned checkout

The public recipient was applied with `configure_subscription.py`, preserving
the existing Sepolia resource. Only the commerce runtime was restarted. Its
managed publisher now advertises immutable 10-USDC offers. The deployed catalog
reports `AVAILABLE` for Atlas Monthly.

Readback checks the actual Arbitrum chain ID, Circle USDC contract, six decimals,
EIP-712 domain and receiving balance. The primary RPC supports finalized state.
An independent PublicNode read is explicitly labelled latest: its free endpoint
rejects archive access. A dRPC finalized archive query also failed. Neither failed
query nor a latest-state observation is treated as payment-finality evidence.

The existing operator account created one verification checkout with an unsigned
placeholder payer. The real deployed merchant returned HTTP 402, x402 v2, exactly
10,000,000 atomic USDC, Arbitrum One and the new recipient. The unsigned checkout
was cancelled and created no entitlement. There was no customer signature,
facilitator settlement, transaction submission or paid subscription purchase.

## Evidence and reproduction

- `artifacts/atlas-release/subscription-wallet-tests.log`: six focused wallet tests.
- `artifacts/atlas-release/subscription-wallet-public.json`: address, generation
  time and key/address consistency; public metadata only.
- `artifacts/atlas-release/subscription-wallet-live.json`: deployed catalog,
  actual service identity permission denial, labelled RPC reads, exact unsigned
  challenge and cancellation. This supersedes the earlier unconfigured merchant
  status in `merchant-compute-live.json`.
- `artifacts/landing-20261003/subscription-active-browser-check.json`: changed
  subscription screen at desktop/mobile widths, layout, images and browser errors.
- `artifacts/atlas-release/manifest.json`: source and public evidence hashes.

Run the wallet tests and live verification on Canada, not on the Mac. Re-running
the provisioner validates/reuses the existing receiver. It does not create a new
address or sign a transaction. Re-running the live verification creates and
cancels another unsigned checkout; it never pays.

The console is at
`https://machine.148-113-153-116.nip.io/commerce/console#subscriptions` and is
linked from `https://skew.deals`. Compute purchasing remains unadmitted; adding
a subscription receiver does not connect or authorize a GPU provider.
