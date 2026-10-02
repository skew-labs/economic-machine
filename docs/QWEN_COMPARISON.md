# Real Qwen comparison: agreements, provider tokens and x402 outcomes

This study measures a policy engine against an actual Qwen3-32B agent loop. It also retains a competent
cached direct code client. The earlier all-code benchmark demonstrated neither token savings nor higher
agreement rates; [that result](COMPARISON.md) is preserved.

## Completed fast-mode study

The first frozen protocol contains 64 independent fixture offers across eight categories and 32 paired
buyer requests. Twenty-four requests have compatible offers; eight are impossible. Two candidates per
feasible category satisfy every condition and cost exactly 0.4 test tokens. Other candidates violate price,
license, purpose, freshness, update cadence or response time. Offers are repeated across requests; this
study does **not** treat every repetition as an independent customer or market.

| Observed measure | Direct Qwen, `/no_think` | Cached direct code | Economic Machine |
| --- | ---: | ---: | ---: |
| Feasible agreements followed by verified payment/delivery | 10/24 (41.7%) | 24/24 (100%) | 24/24 (100%) |
| Correct rejection of eight infeasible requests | 7/8 | 8/8 | 8/8 |
| Agreement decision p50 | 1,421.83 ms | 56.39 ms | 12.41 ms |
| Warm event model calls | 64 | 0 | 0 |
| Tokens including initial policy interpretation | 122,242 | 417 | 417 |
| Provider-reported USD including interpretation | 0.00734812 | 0.00006924 | 0.00006924 |
| Trade discovery/negotiation requests | 256 | 256 | 32 |
| One-time directory/registration requests | 1 | 1 | 65 |

Against this actual per-event Qwen loop, startup-inclusive token reduction is **99.6589%** and
provider-reported model-cost reduction is **99.0577%**. Success rises **58.33 percentage points** in this
fixed workload. The primary agreement deadline was five seconds, declared before calls. Every direct Qwen
decision finished below five seconds: its missed feasible orders are policy-selection failures or abstentions,
not deadline failures. Keeping a 120-second decision deadline leaves its ten valid decisions unchanged.
An unsafe proposed selection is blocked before signing; a safe abstention is not counted as a transaction.

The initial policy compile really used 237 prompt + 180 completion tokens. It is shared by the study, then
charged once to each arm's hypothetical standalone total. The actual experiment made 65 provider calls and
used 122,242 tokens; **do not sum the three hypothetical arm costs as three actual provider charges**.
All attempts, including the first invalid selections and bounded repairs, are counted. Provider usage includes
any billed reasoning tokens. Missing counters/cost remain unknown, never zero. USD is the API's reported
cost, not an independently reconciled provider invoice. Provider prompt caching can affect its price.

The engine has no token or success-rate advantage over the cached deterministic direct client in this book.
An agent that caches decisions for unchanged offers can also avoid repeated inference; this fast-mode study
does not claim those savings as unique to our product. Publisher registration, database/journal work and node
hosting are additional costs. Model-cost reduction is not total operating-cost reduction.

## Method and outcome boundary

Both direct methods cache the category directory once and retrieve all eight category-matching offers in
parallel. Qwen receives every offer in one compact batch, the complete buyer conditions and negotiation
formula. It returns only an ID or abstention. There is at most one repair, provided the first response leaves
time within the primary deadline. The engine receives the same typed policy through its actual FastAPI HTTP
endpoint; its indexed matching uses the existing production code. Arm order is seeded and randomized per
event. All HTTP is real loopback traffic; no delay or synthetic token count is inserted.

The model first compiles a natural-language mandate. Its normalized constraints must exactly match the
predeclared mandate or the entire run aborts. No model response changes a price limit, permission, signer or
recipient. Each arm's choice is revalidated before payment. The comparison counts any compatible agreement;
it does not require the model to choose our preferred tie-break winner to count as successful.

Primary success means a compatible **agreement within five seconds, followed by** verified payment and
delivery. This is not a claim that public-chain finality takes five seconds. Accepted orders enter the same
noncustodial x402 implementation with a separate ephemeral fixture owner signing EIP-712 authorizations.
The test merchant executes actual EIP-3009 transfers in PyEVM; the runtime independently checks authorization,
transfer receipt, test finality, delivery and exact capital accounting. In the first study, all **58** paid
orders have distinct transactions, one signed submission each and a 400,000-atom recipient increase each.
Payer/recipient totals reconcile to 100,000,000 minted test atoms. Unsuccessful/infeasible orders never pay.
These are genuine test-token transfers against a fixture merchant, not customer or public-chain purchases.

The fixed sample's exact paired McNemar value is 0.00012207 (14 machine-only successes, zero direct-only).
Wilson success intervals are 24.47–61.17% for the direct Qwen arm and 86.20–100% for the engine. Repeated
fixture categories violate a general independent-market interpretation: these intervals and the p value
are descriptive, not evidence of a market-wide conversion uplift. There is no customer traffic in this run.

## Thinking-mode robustness study

After preserving the first result, a second protocol was committed **before its calls**: 16 distinct
categories, eight offers each, identical economic constraints, 12 feasible and four infeasible requests.
Each category is used once so an unchanged-offer decision cache cannot answer a later duplicate. Qwen gets
its documented `/think`, temperature 0.6 / top-p 0.95 settings and an 8,192-token output budget. The shared
policy compiler remains in fast mode for both methods. The same five-second primary deadline is retained.

| Observed measure | Direct Qwen, `/think` | Cached direct code | Economic Machine |
| --- | ---: | ---: | ---: |
| Agreement within 5s followed by verified payment/delivery | 0/12 | 12/12 | 12/12 |
| Compatible decisions if deadline is disregarded | 6/12 | 12/12 | 12/12 |
| Compatible decisions within 30s / 120s | 4/12 / 6/12 | 12/12 / 12/12 | 12/12 / 12/12 |
| Decision p50 / p95 | 19,384.14 / 34,244.71 ms | 22.99 / 56.75 ms | 13.47 / 20.72 ms |
| Tokens including initial interpretation | 48,122 | 417 | 417 |
| Provider-reported USD including interpretation | 0.00728628 | 0.00005992 | 0.00005992 |
| Warm model calls | 16 | 0 | 0 |
| Trade requests / one-time setup requests | 128 / 1 | 128 / 1 | 16 / 129 |

Startup-inclusive token reduction is **99.1335%**; provider-reported model-cost reduction is **99.1776%**.
The five-second outcome advantage is conditional on that predeclared service budget. It is not evidence that
every inference-assisted agent fails to transact: six compatible choices arrive late, and a system with a
longer budget or different repair/caching policy has a different outcome. Those late choices were not paid.
Late inferences were allowed to finish to observe complete provider usage. An agent that cancels inference
at five seconds may consume fewer tokens; cancellation billing was not measured or estimated here.
All four infeasible requests are correctly rejected. The engine and deterministic direct arm each pay and
verify 12 orders, with 24 distinct test-token receipts in this completed run. Cold rule registration takes
6.09 seconds; no cold-start latency superiority is asserted.

An earlier attempt of this same robustness workload stopped after nine events because the ephemeral
PyEVM latest-block clock lagged behind the real model wait. The short authorization used that stale clock,
and the payment runtime retained an UNKNOWN hold. The fixture now synchronizes its test chain
before issuing an authorization. A 300-second clock-gap regression and the full corrected workload pass.
No quote rules, prompts, deadlines or sampling settings were changed to improve the result. The failed
attempt's [protocol and usage](../artifacts/qwen-comparison-20261002-thinking/failure.json) remain: **10 calls,
27,019 tokens, USD 0.00409704**. It is excluded from completed-trade statistics, not from experiment spending.

Across both completed studies **82** paid test-token orders reconcile. Including the failed attempt, the
actual research run made **92 provider calls, 197,383 tokens, API-reported USD 0.01873144**. These are observed
experiment totals, not the sum of hypothetical standalone arm costs. Seventeen new unit/integration tests
pass; four types of corrupted evidence are rejected against each completed study. Existing unchanged
contract/core checks were not rerun.

Thinking study: [result](../artifacts/qwen-comparison-20261002-thinking-v2/result.json),
[protocol](../artifacts/qwen-comparison-20261002-thinking-v2/protocol.json),
[provider calls](../artifacts/qwen-comparison-20261002-thinking-v2/provider-calls.jsonl),
[independent recalculation](../artifacts/qwen-comparison-20261002-thinking-v2/audit.json).

## Reproduction and evidence

Run compute only on the authorized Canada server. Credentials remain in the existing protected server file;
never put a key on the command line, into Git, or in chat.

```sh
sudo .venv/bin/python scripts/compare_qwen.py \
  --key-file /srv/skew/gwdc-financial-agent-20260924/secrets/kiln.key \
  --output artifacts/new-unique-run-directory
sudo .venv/bin/python scripts/audit_qwen_comparison.py artifacts/new-unique-run-directory
.venv/bin/python scripts/check_qwen_audit.py artifacts/new-unique-run-directory
```

Every directory holds the protocol, its canonical SHA-256 commitment written before the first model call,
provider request IDs/model IDs/usage, request and final-output hashes, append-only per-trade records,
source snapshots, selected offers, late/invalid outcomes, x402 receipts and wallet/account reconciliation.
Raw model reasoning, secrets and payment signatures are not persisted. The independent evidence auditor
recalculates monetary and nonmonetary eligibility without importing the engine's negotiation predicate,
checks every billed attempt's allocation and matches paid counts to exact observed balances. Its negative
checks reject changed source, altered billing, invalid selections and inconsistent wallet totals.

First study: [result](../artifacts/qwen-comparison-20261002-run1/result.json),
[protocol](../artifacts/qwen-comparison-20261002-run1/protocol.json),
[provider calls](../artifacts/qwen-comparison-20261002-run1/provider-calls.jsonl),
[independent recalculation](../artifacts/qwen-comparison-20261002-run1/audit.json).
Full source snapshots and isolated SQLite stores remain on the remote host.

For the robustness run, add `--thinking --events 16 --unique-events` to the benchmark command. For remote
store-to-store agreement verification, add `--verify-stores` to the auditor command. This compares the
actual HTTP engine's economic agreement fields with the downstream payment fixture; only isolated
buyer/seller/record identities differ. Sources and SQLite stores are kept remote; copied JSON evidence
alone supports the independent policy/token/outcome recalculation without those full stores.

Sampling/mode choices follow the [official Qwen3-32B model card](https://huggingface.co/Qwen/Qwen3-32B).
Usage fields and reported USD follow the [Kiln chat-completions reference](https://kiln.bricksum.com/docs/en/api-reference/chat-completions).
The model API currently lists `qwen3-32b`; the experiment records what model ID was actually returned.
