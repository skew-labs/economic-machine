# SKEW — Arbitrum submission demo

Owner scope correction, 2026-10-04: this is the **Arbitrum Open House** film.
Amazon, Alexa, Bedrock configuration, PayPal and contact-list cleanup are outside
this recording. Qwen3 32B is the currently selected conversational provider.

## One story

**An agent buys the data it needs within its owner's rules, receives an Arbitrum
DataPass license, and gets the exact purchased data.**

The first-person viewer is the buyer's owner. The buyer agent requests a usable
data product; the supplier provides a specific licensed version. Engine checks
conditions and controls transaction state. DataPass records the purchased right
on Arbitrum. Mining supplies verified, publishable work and has its own SKEW
issuance rule. These are roles in one product, not four product pitches.

Example opening request, to bind to the actual admitted product before filming:

> “Buy the verified analysis data my agent needs, within my budget. Show me the
> version, permitted use and total price before I approve.”

Use the actual release name, version, currency and price shown by the product.
Do not invent a catalog item, seller or price merely for this sentence. Atlas is
an available data category; it is not automatically the output of route mining.

## Main cut: 2 minutes 50 seconds

| Time | Screen and action | English narration |
| --- | --- | --- |
| 0:00–0:15 | Console: one buyer request; show the exact budget and usage condition | “SKEW lets agents buy data and services under explicit budgets and rules. Here, an agent needs one verified data product.” |
| 0:15–0:40 | Data licenses: one real release, source/version, permitted use and quoted total | “The engine checks the offer's price, version and usage conditions. The owner can see exactly what the agent is asking to buy.” |
| 0:40–0:55 | Show the same offer rejected under an insufficient budget | “This offer exceeds the policy, so the engine stops before payment.” |
| 0:55–1:25 | Restore the intended budget; show exact approval and the actual purchase transaction | “The owner approves the purchase. Payment and license issuance happen on Arbitrum, with the result checked against the approved terms.” |
| 1:25–1:50 | Open the matching DataPass token, buyer address, release and validity | “This is the buyer's access right to this data version. A transaction hash alone is not enough: we check the license and its current state.” |
| 1:50–2:15 | Deliver the file/API response; show its content hash and linked purchase receipt | “The agent receives the exact purchased artifact. Its hash matches the licensed release.” |
| 2:15–2:30 | Reopen that same purchase and result, without starting another payment | “The purchase has durable state. Returning to the app resumes that record instead of charging again.” |
| 2:30–2:50 | Show the supply side: verified work, its matching publication and the actual SKEW reward receipt if available | “The supply side rewards verified, publishable work with SKEW. This token reward is separate from the buyer's payment.” |

End on the delivered result and its Arbitrum receipt. Do not finish on a broad
roadmap or a wall of module names. Use the real wallet signature recording or
clearly label a playback of a previously completed transaction.

## Required continuity

One buyer and one release must remain identifiable across approval, transaction,
DataPass entitlement and delivered bytes. Show the same purchase ID where the
implementation provides it. A new wallet login is not payment approval.

Use one payment rail for the demonstrated purchase. A native DataPass purchase
uses the license contract's ERC-20 payment and issuance. Existing x402 settlement
is a separate service-payment path; do not imply that an x402 transfer itself
mints a DataPass or charge the same item through both paths.

For the mining ending, the verified work must match the displayed release's
artifact and rights commitments. The current SkewArtifactMining claim rule needs
winning work and matching active publication, **not proof that a customer bought
it**. Do not narrate a customer purchase as the trigger for SKEW issuance. A route
result and the Atlas price report cannot be presented as the same artifact.

Fuel belongs only in a short contextual cut if the buyer actually needs gas.
Show a verified gas-receipt result; do not claim USDC eliminates the native-gas
requirement. Omit Fuel entirely when the buyer already has sufficient ETH.

## What is evidence and what remains a target

This script defines the desired native DataPass film; writing it does not complete
the following transactions. Check the current deployment and runtime before recording.

| Stage | Saved evidence inspected for this revision | Needed for the new main cut |
| --- | --- | --- |
| Qwen intent | Actual public Qwen3 32B response and persisted history | Record the product-specific request and resulting supported action |
| Budget rejection | Historical x402 policy replay | Verify the guard for the exact purchase path in the film; label replay if used |
| Project contract | Saved finalized DataPass deployment on Arbitrum Sepolia | Confirm the exact chain/address used throughout the new purchase |
| Native DataPass delivery | Purchase and entitlement code; deployment proof explicitly records no purchase | Actual release, buyer-approved payment, finalized entitlement and delivered hash |
| Mainnet launch | Unsigned review and corrected MetaMask preflight | Owner signature and independently reconciled deployment; no receipt is inferred from this review |
| SKEW reward | Compiled contract semantics and tests | Matching job/publication and real claim event plus token-balance readback |

The inspected DataPass browser controls still contain Sepolia/test-USDC labels
and Sepolia explorer links. A mainnet deployment alone does not migrate that
purchase flow. Before filming mainnet, wire the exact deployment, chain, asset and
release into the purchase and delivery path and verify them together.

If those new receipts are not available, use the existing **historical Sepolia
x402 purchase** honestly: [recorded-purchase walkthrough](ARBITRUM_SEPOLIA_REPLAY_SCRIPT.md).
It proves a 0.01 test-USDC service purchase and delivered chain snapshot. It is
neither a DataPass purchase nor a mining payout. Do not splice its receipt into
the mainnet DataPass story.

## Filming and judge links

- Product: https://skew.deals/commerce/console
- Data licenses: https://skew.deals/commerce/console#data
- Mining supply view: https://skew.deals/commerce/console#mining
- Historical settled purchase: https://skew.deals/commerce/submission
- Source: https://github.com/skew-labs/economic-machine

Keep the network label visible whenever showing money, contracts or tokens.
Record the finalized transaction hash, buyer entitlement and delivered file as
separate evidence. Preserve private keys, API credentials and unrevealed salts off
screen. The three-minute format is an editorial choice, not a claim about the
organizer's current video rules or submission status.
