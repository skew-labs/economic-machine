# SKEW: one product, one transaction

Prepared 2026-10-04. This is a recording script and operator handoff, not a new
deployment, model-response or purchase receipt. The latest code is on the stacked
PR12–PR16 branches; those PRs are not represented as merged.

## Product sentence

**SKEW lets agents buy the data and services they need, within a budget, and track
the purchase through delivery.**

한국어: **SKEW는 에이전트가 필요한 데이터·서비스를 예산 안에서 구매하고,
전달까지 확인하는 거래 실행 소프트웨어다.**

The buyer is an agent developer or operations team. The user outcome is a usable
result and a traceable purchase, not a collection of separate infrastructure demos.
The open-source console is the control surface. Economic Machine enforces budgets,
approvals and recovery. DataPass supplies versioned data licenses. Mining supplies
verified, publishable results. Fuel helps acquire gas when necessary. Bedrock
interprets requests; it does not sign or authorize purchases.

Do not introduce TRON allocation, derivatives, GPU RWA, random-graph research or
hardware optimization in this demonstration. GPU pricing can be a data category,
but does not define the product. This is a presentation decision, not deletion of
existing software or a claim that every module shares one completed AgentRun.

## Primary Arbitrum cut: under three minutes

The story is **one agent buying one explicitly licensed data version**. Use one
buyer, one seller, one budget, one result and one trace. Show the price and network
on every payment screen. Avoid a dashboard tour.

| Time | What the viewer sees | English narration |
| --- | --- | --- |
| 0:00–0:15 | The console and the single buyer request | “An agent can find information. Buying it safely from another service is harder. SKEW turns that purchase into a controlled, recoverable workflow.” |
| 0:15–0:40 | Exact data version, freshness, permitted use, price and budget | “The buyer specifies what it needs and what it may spend. The engine checks the offer against those rules.” |
| 0:40–1:00 | One rejected over-budget offer, then the eligible offer | “An attractive result does not override the budget. This rejected plan cannot become a payment.” |
| 1:00–1:30 | Exact purchase approval and its actual chain receipt | “The owner approves the exact purchase. The engine waits for confirmed settlement before treating the payment as complete.” |
| 1:30–1:55 | Delivered file, version and hash; license entitlement for a DataPass purchase | “This is the result the buyer received. Its version and hash match the purchase, and access is checked against the purchased rights.” |
| 1:55–2:15 | Reload/resume or recorded recovery for this same transaction | “A refresh does not create another purchase. We resume the same transaction and its delivery state.” |
| 2:15–2:40 | Supply side: the artifact, its verification and publication; SKEW receipt only if one exists | “Providers can contribute verifiable work. A winning result that meets the publication rules can earn SKEW. Token issuance is separate from customer revenue.” |
| 2:40–2:50 | Return to the received result | “One engine for the rules, one record of the purchase, and a result the agent can use.” |

The full script above is the target cut. A new mainnet DataPass purchase and a
mining reward must not be acted out as completed before their own receipts exist.
The currently verified fallback is the historical **Arbitrum Sepolia x402 purchase**
in `../ARBITRUM_DEMO_SCRIPT.md`: 0.01 test USDC, delivered chain-snapshot JSON,
payment state and independently checked transaction. In that cut, say “service
payment and delivery”, not “a newly minted DataPass license”. Label the recorded
testnet transaction throughout. Do not splice the old receipt into a new order.

For the mainnet cut, use the exact new contract addresses, release, buyer and
transaction hashes from reconciliation. Payment amount and permitted use must be
shown to the buyer before signing. Do not spend the owner's remaining USDC merely
to complete the recording. Operator rehearsal and historical replay need no new
payment.

## One payment rail per purchase

Native DataPass purchase: approved ERC-20 spend → contract purchase → finalized
license entitlement → authenticated file delivery and hash check.

x402 service purchase: payment challenge → scoped authorization → confirmed
settlement → service delivery. An x402 payment does not itself mint a DataPass.
Do not make the customer pay through both paths for the same item. Introduce x402
in the architecture explanation or show its existing evidence as an appendix.

## Amazon cut: the same execution boundary, a different user entry

Keep the Amazon recording focused on “delegate → compare → approve → leave →
return to the completed work”. Do not spend half of the film explaining mining,
token supply, wallet gas or PayPal configuration.

The currently runnable task is business contact-list cleanup. Example request:

> “Prepare this contact list for our CRM import. Show me what will change, remove
> exact duplicates only after I approve, and keep the completed file for later.”

1. Show the editable request and the actual input. State that this is an Alexa+
   style web simulation; browser dictation is optional and permission-dependent.
2. Compare the two implemented plans: trim and preserve all rows; trim and remove
   exact duplicates. Show their actual output counts and zero external-service fee.
3. Approve one frozen plan. Open the real output file, not a model-written success
   message. Infrastructure cost is not included in the zero service fee.
4. Reload the console, choose “Resume my work”, and reopen that same result.
5. Once authorized Bedrock access succeeds, record the actual provider/model and
   correlated tool trace. Until then use the direct controls and explicitly label
   the AWS blocker. A stubbed SDK test is not a live model response.

The existing 44-second recording is
`../../artifacts/amazon-completion/pr16/amazon-workflow.mp4`. It already discloses
the web simulation and blocked AWS access. It is not a proof of live Alexa or
Bedrock invocation. Wider research, procurement, enrichment and localization are
future demonstrations until their actual fulfillment providers are connected.

## What the owner needs to do now

### Bedrock

The saved preflight returned HTTP 403 and an explicit SCP denial of
`bedrock:CallWithBearerToken`. The server is configured for `us-east-1` and
`amazon.nova-lite-v1:0`. Qwen fallback is off.

1. Open [AWS Organizations](https://console.aws.amazon.com/organizations/) using
   an authorized organization-management identity.
2. Check Policies → Service control policies and the policies attached to this
   account and its parent organizational units/root. The responsible administrator
   must adjust the applicable denial or its intended exception for this workload.
   Do not remove all organization protections.
3. Adding an IAM Allow or creating another API key cannot override an explicit SCP
   Deny. If this is a supplied lab/company account, request the change from its
   administrator instead of trying another region to evade the policy.
4. After that change, rerun the bounded Converse preflight from the Canadian host.
   Only after a successful real response should the operator clear the cached
   `ORGANIZATION_DENY` status and verify the console/tool trace. Remaining IAM/model
   access requirements, if any, must be resolved from the new response.

Administrator request:

> Our SKEW workload receives an explicit SCP denial for
> `bedrock:CallWithBearerToken` when invoking Amazon Bedrock in us-east-1. Please
> review the policies applied to this account, including inherited policies, and
> permit the approved workload's intended API-key use and required model inference
> permissions while retaining unrelated controls.

References: [Bedrock API-key permission controls](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-permissions.html),
[updating an SCP](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_policies_update.html),
[SCP evaluation](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_scps.html).
Do not put the bearer token in the recording, repository or support request.

### Mainnet deployment

Open [the owner launch review](https://skew.deals/commerce/launch) in the MetaMask
browser. Owner: `0x1c8F822682DDAbFF7dA1A7D5D881bdA96c8F41C5`; network: Arbitrum One
42161. Review and sign the deployment yourself. The program never holds an owner
key. The unsigned review expires after five minutes and must be refreshed by the
operator if expired. A signature must not be retried blindly after an unknown
submission outcome.

The bundle creates DataPass, the artifact-mining verifier and its SKEW token.
Initial supply is zero; cap is 160,000 SKEW; one accepted job can earn one SKEW.
There is no owner mint. Deployment consumes ETH gas, not USDC. The configured
review ceiling is 0.0005 ETH; the current displayed quote is the tighter bound.

After signing, reconcile the hash against both RPCs: exact creation input, owner,
nonce, finalized receipt, deployed runtime and configuration. Deployment is not
the first reward or sale. Those require a published, rights-bound release,
completed winning work, token claim, and separately approved buyer transaction.

## Recording gates and current evidence

| Claim | Evidence already available | Additional evidence required |
| --- | --- | --- |
| Public console works | Remote public-domain workflow: two plans, approval, actual output, reload | None for the recorded local cleanup task |
| Bedrock produced a response | SDK tests and a real 403 preflight | Authorized successful Converse response and console tool trace |
| Mainnet contract exists | Compiled artifact and independently quoted unsigned creation | Owner signature, final receipt, two-RPC runtime/config readback |
| Miners earned SKEW | Contract semantics and automated contract tests | Actual accepted work, matching publication and finalized reward receipt |
| DataPass purchase delivered | Contract/admission/delivery implementation and earlier evidence | A receipt and delivered hash for the exact release shown in the new film |
| Testnet service purchase delivered | Existing Sepolia x402 transaction and immutable delivered JSON | Clearly identify this as historical testnet evidence |
| Amazon application submitted | Submission materials and local video | Public required video URL and successful submission readback |

Lead with verified customer outcomes. Keep unexecuted stages visibly marked.
Do not advertise token price, guaranteed earnings, mainnet production safety or
an independent contract audit from compilation or unit tests.
