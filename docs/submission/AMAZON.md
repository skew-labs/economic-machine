# SKEW — delegated work with an exact approval boundary

## Submission copy

SKEW turns a request into work that can be reviewed, approved, resumed and verified. The same conversation connects a person's accounts, agents, task budgets and execution records. A language model interprets intent; a deterministic engine owns payment constraints and delivery state.

Our Alexa+ track experience is a **web simulation**, not a deployed Alexa skill. A person can dictate a request into an editable transcript, retain confirmed preferences, review work plans and return to the same result later. The runnable demonstration cleans a business contact list: compare preserving all rows against removing exact duplicates, approve one transformation, download the actual file, then reload and resume the saved result. Both plans have no external service fee; infrastructure cost is not included. GPU pricing is not required.

The integration uses Strands, Amazon Bedrock and authenticated MCP 2025-11-25 Streamable HTTP. The model has bounded read tools and no payment approval tool. Economic Machine keeps plan hashes, explicit consent, payment uncertainty and verified results. A signed swap is tracked to a finalized chain receipt; uncertain payments are reconciled rather than blindly charged again.

## Evidence and current access

- Public product: https://skew.deals/commerce/console
- Code: https://github.com/skew-labs/economic-machine
- Original scope: `docs/AMAZON_PAYPAL_PLAN_20261003.md`, six planned PRs.
- This contribution: GitHub PRs 12–16, stacked and reviewable.
- Local task work and HTTP MCP are implemented and exercised remotely.
- Bedrock bearer access is blocked by AWS organization's explicit SCP denial. The SDK tool-loop check uses stubbed Converse responses. **Do not describe it as successful live Bedrock inference.**
- Dictation depends on browser support and permission. No Alexa device or private-preview runtime was used.
- No actual PayPal capture/refund occurred in this contribution. The sandbox adapter is implemented but merchant credentials are absent.

## Demonstration order

1. Introduce the user's goal: finish a contact import, keep control of transformations and cost.
2. Show two actual plans and how they differ.
3. Approve deduplication and open/download the real output.
4. Reload, resume the persisted result and show its hash.
5. Show MCP tools and explain that an agent cannot approve its own payment.
6. Label the AWS access blocker and simulated Alexa experience clearly.

A 44-second English-captioned film of the real workflow is included in `artifacts/amazon-completion/pr16/amazon-workflow.mp4`. It labels the web simulation and AWS access blocker. Review and upload it to YouTube/Vimeo for the required submission URL; a video file in the repository is not that URL.

## Release gate

Do not mark submission complete without successful Devpost submission readback. Public source/license, free judge access, contributions, project feedback and video URL must all be verified. AWS mini-challenge service-use evidence is blocked until authorized Bedrock access succeeds. Mainnet token launch is a separate owner-approved release, not an Amazon prerequisite.

Official rules: https://amazonappdev2026.devpost.com/rules
Official FAQ: https://amazonappdev2026.devpost.com/details/faqs
