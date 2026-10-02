# Economic Machine product portal

Implemented October 2, 2026. English product copy; the owner's Skew reference supplies only the white/slate palette, broad spacing, pill navigation and large typography. The generated machine illustration was removed. The hero is now a product surface fed by the same live comparison as the provider table; no artificial photograph remains.

## Entry points

- Landing: https://skew-economic-machine.angus4314.chatgpt.site
- Canada mirror: https://machine.148-113-153-116.nip.io/commerce/
- Management: https://machine.148-113-153-116.nip.io/commerce/console
- Recorded workspace: https://machine.148-113-153-116.nip.io/commerce/console?preview=1

Sites is owner-private; its existing audience has not been expanded. The existing HTTPS Canada host serves the product route and authenticated console. Source for the landing lives separately at `/Users/heoun/economic-machine-site`; the deployment commit is recorded separately in `artifacts/wallet-design/site-deployment.json`.

## Customer-visible demonstration

- Six explicitly named example providers use original symbols, not unaffiliated companies' logos.
- Standard policy returns two agreements. A lower price cap returns zero; a five-second freshness limit returns one.
- One shared 0.20 test-credit policy admits a 0.12 reservation and rejects a second 0.12 request. A replay retains one order and one hold.
- A simulated delivery deadline releases the unused 0.12 test-credit reservation exactly once. It does not reverse an on-chain payment.
- Every example executes the existing Market/Store engine in a disposable SQLite directory. No signer, external model or settlement client is attached.
- NVIDIA Inception membership is displayed based on the owner's explicit confirmation. This is membership wording, not a claim of investment, product endorsement or a different partnership tier. No unofficial member badge is reproduced.
- Measured HTTP and Qwen results retain their separate scopes in collapsed details and `evidence.json`. Existing experiments were not rerun.

## Console connection

The existing console code is preserved. A prefix-aware transport sends authenticated calls to `/commerce/api/`, which proxies to the existing production-mode runtime on loopback 4261. HTTPS and scoped HttpOnly/Secure/SameSite cookies remain enforced. Public console sign-in now uses a wallet signature; legacy provisioned operator access remains available for existing server-side recovery tooling. Catalog loading follows authentication; reopening a session does not reset capital.

The preview parameter only selects the immutable, public testnet record. It does not authorize runtime access. Recorded workspace data comes from an explicit field allowlist of the prior verified Sepolia proof; no API keys, operator passwords, signatures or wallet material are returned. Preview writes are rejected, and credentials remain private. Confirmed 0.01 test-USDC spending and the actual Arbiscan receipt are inspectable.

The new portal service is `machine-commerce-portal.service`, bound to loopback 4270 with a dynamic user, private temporary storage, read-only filesystem, a 256 MiB memory limit and bounded concurrency. Public scenario input accepts one enum field and at most 2048 bytes. Credential-free CORS is confined to the scenario endpoint. Static paths are allowlisted.

Nginx includes `infra/portal.nginx.conf` additively. The existing root application on 4190 and POST-only Sepolia merchant endpoint on 4262 remain intact. The portal never loads the merchant wallet key.

## Verification

All code checks ran on the authorized Canadian host in `/srv/skew/economic-machine-commerce-20261002`. The Mac was used only for source edits, lightweight reads, remote orchestration and browser inspection.

- 13 new portal tests passed: six-provider matching, policy changes, budget reuse, identity-preserving replay, deadline recovery, isolation, closed input, request limits, CORS boundaries, static console paths and public-record sanitization.
- Ruff and syntax checks passed for the new Python files and changed JavaScript.
- HTTPS connection verification passed for rejected anonymous management, operator login, scoped cookies, workspace/keys/payments/catalog reads, session resume, all five public scenarios and static assets. No key, mandate, signature or payment was created by this check.
- Browser checks confirmed all three comparison outcomes, both capital scenarios, six rendered provider symbols, working evidence tabs, login protection and recorded payment/receipt access. At 390 px the landing and console have no horizontal overflow.
- Source and archive were prepared on Canada with the Sites workflow; the exact pushed commit was deployed successfully.

Durable backend evidence is `artifacts/landing/connection.json` and `artifacts/landing/tests.txt`. No new real-money transaction or testnet broadcast occurred in this work.

## Wallet login and revised visuals

The console discovers installed Ethereum providers with EIP-6963, passes account selection and signing to the selected provider, and sends the signature to the HTTPS backend. The server creates the entire EIP-4361 message with its exact origin, console URI, chain, 32-character nonce, issued/expiry times and request ID. A five-minute challenge is bound to an HttpOnly browser cookie. EIP-191 recovery must equal the requested EOA. A durable exclusive transaction consumes the challenge once and rotates the owner session. Replay, wrong address, edited domain/URI/chain/statement, expiry, malformed signatures, missing binding and foreign/missing Origin are rejected. Durable throttles bound nonce issuance and verification.

A wallet owns only its separate zero-capital workspace. Successful login does not copy the existing operator workspace, seed money, grant payment signing authority or create an agent key. Returning wallets retain their workspace and limits across supported EOA networks (Ethereum, Arbitrum One and Arbitrum Sepolia). Signing out invalidates the owner cookie; agent keys remain until expiry or explicit revocation. The current connector supports injected EOA wallets, not EIP-1271 smart accounts or a WalletConnect QR session. Connected account/network changes during signing abort login; changes after a connection sign out the owner.

The hero and provider table read one engine result, including its actual policy age/price limits. Installed wallet icons are isolated image elements; remote icon URLs and arbitrary markup are rejected. A locally served official Phantom image is a presentation fallback when its announcement lacks a usable data icon, never a provider identity check. Official NVIDIA artwork is shown unchanged with the owner-confirmed Inception membership wording. Logos, button heights and icon geometry use common dimensions; the old illustration is absent from the static bundle. Console asset URLs carry a version to prevent old cached styles from mixing with wallet markup.

Asset provenance: NVIDIA `https://www.nvidia.com/etc/designs/nvidiaGDC/clientlibs_base/images/NVIDIA-Logo.svg`; Phantom `https://phantom.com/_web_platform_assets/favicon-96x96.png` (linked by its official homepage), retrieved 2026-10-02. Fixture-provider marks are original and do not imply third-party integrations.

Remote verification: 36 relevant Python tests (14 wallet, 9 operations, 13 portal), four wallet-client tests, Ruff and JavaScript syntax checks passed. The changed portal paths were rechecked after adding cache versioning and console CSP. A disposable, unfunded EOA completed challenge → signature verification → authenticated workspace/keys/payments reads → sign-out → same-identity relogin over the deployed HTTPS route. No customer extension signature, agent key issuance or payment/broadcast was performed by the live probe. Evidence is in `artifacts/wallet-design/`; prior payment and model evidence remains historical. Browser QA covers the installed MetaMask/Phantom selection screen and 1440/390 px layouts.
