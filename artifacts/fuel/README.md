# Fuel acceptance evidence — 2026-10-04

All builds, tests, mainnet reads and browser checks ran on the authorized Canadian
server. The Mac was used for source edits and remote orchestration.

| Evidence | Result | What it establishes |
| --- | --- | --- |
| `python-tests.log` | 19 passed | Swap safety, independent price guard, expiry/finality, restart recovery, engine budgets and permissions |
| `engine-http-tests.log` | 1 passed | Scoped agent proposal, owner-only signed submission, receipt and budget charge through actual HTTP routes with a fake provider |
| `browser-wallet-tests.log` | 4 passed | EIP-712/ABI reconstruction, tampering, wallet change and storage-failure guards |
| `integration-tests.log` | 24 passed | Existing access and connection regressions |
| `live-readonly-probe.json` | Verified mainnet quote | Actual CoW simulation using an unfunded ephemeral signer, public contract/RPC/feed checks, zero order submissions |
| `browser-verification.json` | 4 checks passed | Public desktop/mobile rendering, actual quote HTTP path, invalidating an edited review, unauthenticated engine denial |
| `deployment.json` | Matching SHA-256 | Release sources and installed service/nginx configuration |
| `github-verification.json` | 6 critical files match | Independently downloaded public GitHub sources equal the deployed router, guard, store, budget engine and browser signature code |

The static source checker passed for the new Python modules, tests and scripts.
The 48 tests above are distinct cases across focused runs, not a repeated full
repository suite. The public browser run used a **synthetic read-only wallet**;
it neither connected the customer's extension nor requested any signature.

`deployed-source-tests.log` additionally repeats only the 20 new Fuel cases with
imports forced to the isolated deployed release. This checks compatibility with
the production baseline rather than unrelated uncommitted DataPass changes. All
20 passed; this rerun does not increase the distinct total of 48 cases.

The first public browser check caught the DynamicUser StateDirectory symlink
being rejected by the private store. The service now uses its `data/` subdirectory;
the security check stayed enabled. A subsequent attempt started before the
restarted service was ready; the completed recorded run passed once ready.

Screenshots are actual public renders. `quote-review.png` masks the public wallet
address and uses the explicitly labeled QA provider. Its quote is a historical
estimate, not a trade, receipt, or current executable price.

**Customer financial signatures: 0. Orders submitted: 0. Transactions broadcast:
0. Customer finalized swaps: 0. Independent audit: not performed.**

These artifacts do not prove mining issuance, DataPass sale/delivery, automatic
execution of the parent task, arbitrary wallet compatibility, or free gas.
