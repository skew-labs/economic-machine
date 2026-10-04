# One workspace, one conversation

The console is the product's working surface. Overview opens a persistent
conversation; actions, wallet review and recovery stay in the same workspace.
The landing page remains a separate public introduction on the same domain.

Visual direction: Paper #FFFFFF, Mist #F4F6F9, Ink #192434, Slate #64748B,
Line #DEE4EC and Cobalt #275DCC. Native system sans typography, 16px reading
text, a quiet 224px navigation rail and a 760px conversation column. No
decorative gradients, simulated activity counters or hero illustrations.
Action cards are subordinate to conversation, with explicit spending limits.

The Amazon six-PR plan's compiler boundary remains: Strands + Amazon Bedrock interpret a request;
the Economic Machine validates typed plans. STA's validated-draft and exact approval pattern is reused; its Solana signer is not exposed to this Arbitrum workspace.
The owner's interim selection is Qwen3 32B through the existing Kiln endpoint.
The Bedrock adapter remains selectable; live Bedrock evidence requires an AWS
account. Neither provider's credentials are sent to the browser.
Confirmed task preferences are retrieved from the owner's workspace, not
invented from conversational guesses. No LLM response grants execution rights.

Wallet login selects Arbitrum One and signs an authentication message. Swap
review uses the existing CoW/USDC permit verifier, two signatures, durable
submission identity and independent chain readback. Unknown settlement keeps
the existing order recoverable. Neither chat nor a retry creates replacement
financial authority.

Server model credentials never reach the browser. Chat requires an owner
session, reserves a bounded daily call slot before inference, persists the
request identity and records model/token/latency evidence. A failed provider
request is not silently replaced with a fake assistant response.

## Release verification — 2026-10-04

Public console: https://skew.deals/commerce/console#overview. The existing landing
is retained; Evidence opens `/commerce/evidence.html`, not the legacy landing.
The custom domain is public. The Sites deployment is version 17, source commit
`bec75979420429867abc778524b6219ad81939e9`. Its commerce proxy permits only the
fixed backend; unrelated Sites cookies are dropped. The surrounding worker
serves only `/`, `/home/*`, `/favicon.ico` and the commerce proxy.

The active model is **Qwen3 32B**. A real public-domain authentication → message
→ model response → explicit task acceptance → reload → logout flow passed.
Its trace recorded 388 input and 126 output tokens, 2,503 ms model latency and
`READY_TO_PLAN`. Authentication used an ephemeral unfunded test identity;
this is neither a purchase nor proof that the requested research was delivered.
The Bedrock bearer probe in us-east-1 was explicitly denied by an organization
service control policy. No other region was tried to evade the denial. The
temporary bearer configuration was removed; Bedrock is not active.

Fuel has no fixed 3-USDC quote ceiling or 27-USDC policy ceiling. Quote amounts
are exact decimal strings through parsing, ABI fields and signature validation.
USDC supports six fractional digits and uint256 representation. Shared-budget
accounting still uses safe JSON integers; user-defined caps and wallet inventory
remain enforced. Existing policies are not automatically enlarged.

`FRESH_CHAIN_STATE_REQUIRED` had a timing bug: a clock captured before sequential
RPC reads could classify a newly mined second-provider block as being in the
future. The clock is now sampled after each read and every observation is aged
again before return. Stale/future blocks, a recovering sequencer, invalid prices
and provider disagreement still block the route.

The operator-supplied paid QuickNode connection is server-only and loaded by
both the runtime and Fuel service, with PublicNode as an independent verifier.
The configuration is root-only (0600); receipt fields contain source roles rather
than URLs. No endpoint credentials are committed or sent to the browser.

Actual public `/commerce/swap-api/quote` evidence for **10 USDC** returned HTTP 200,
`PERMIT_REQUIRED`, estimated `3705763736868338` wei ETH and `14033` USDC atoms
routing cost, in 7,292 ms. Both price observations were fresh. These values are
historical quotes, not current execution guarantees. Financial signatures,
order submissions and chain writes in this verification: **zero**.

Changed-code verification on the owner-approved Canadian host:

- 30 assistant/router tests passed; the initial combined invocation could not
  import the engine suite because `tests` was absent from PYTHONPATH. Only that
  suite was rerun with the correct path: all 11 engine tests passed.
- Six swap-signature/decimal tests passed, including values above JavaScript's
  safe-integer boundary, mismatched amounts and unchanged signature safeguards.
- Eighteen wallet tests passed, including network changes, account changes,
  cancellation and provider UUID rotation. Provider metadata is only a selection
  hint; restoration still checks the current wallet account and chain.
- Two fixed-upstream/domain proxy tests and the portal route test passed earlier;
  neither was repeated after unrelated Fuel edits.
- Browser fixtures rejected superseded cards, consumed approvals and duplicate
  submissions. Their mocked signatures are not financial-execution evidence.
- Public-browser responsive checks and evidence artifacts are stored in
  `artifacts/console-agent-20261004/`.

Durable server evidence is in
`/srv/skew/economic-machine-commerce-20261002/build/console-agent-20261004/evidence`.
The release retains the Fuel database and journals. Source/config rollback is in
`releases/console-fuel-rpc-before-20261004`; restoring code must not remove pending
order records. Public customer wallet approval, order-book acceptance and a
finalized customer fill remain separate from the unsigned checks above.
