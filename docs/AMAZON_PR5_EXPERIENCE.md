# Amazon plan PR5: one conversation, durable work

`AMAZON_PAYPAL_PLAN_20261003.md` is the six-PR competition plan. Its PR5 is the Bedrock/Strands experience, not GitHub pull request #5.

The hosted console selects Bedrock without automatic Qwen fallback. Credentials remain in the private service environment. Strands has three model calls and four read tools per request, bounded output and timeouts, plus global daily attempt limits. Account status, confirmed preferences and completed work are read from the owner's workspace. The model proposes; owner routes authorize. Browser dictation is optional, user-started, and edits the text box without sending or spending. It is not an Alexa device integration; the browser's speech provider handles audio under its own permissions.

## Runnable practical workflow

In Overview choose **Clean business data**, paste a CSV, compare two real transformations (trim all rows or trim and remove exact duplicates), approve one, download the verified result, reload, and choose **Resume my work**. Both plans charge no external service fee; local hosting/compute cost is not included in that price. These are execution plans, not two different sellers. The CSV worker does not verify the truth of input data.

The same workspace retains business task templates, prices, confirmed conditions, paid purchase recovery, local results and swap tracking. Paid task cancellation/refund controls are now visible. Other task categories need a separately admitted fulfillment worker; they are not represented as generated finished reports.

## Model-access boundary

The supplied Bedrock bearer token was privately configured in us-east-1. AWS returned HTTP 403 with an explicit service-control-policy denial for bedrock:CallWithBearerToken. `SKEW_BEDROCK_ACCESS_STATUS=ORGANIZATION_DENY` stops repeated denied calls. An authorized AWS organization administrator must remove that denial; then the operator must re-run an authorized live preflight and clear the gate. Do not bypass it with other accounts or regions. No successful provider output or AWS mini-challenge eligibility is claimed.

The real Strands SDK test runs a tool cycle with **stubbed** Converse responses and verifies call bounds and tool usage. It caught and fixed an incompatible `region_name` plus `boto_session` configuration. This is SDK integration evidence, not actual AWS inference. Browser evidence uses the real deterministic task engine and no model response.
