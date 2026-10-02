# Economic Machine — two-minute demo

Use the public HTTPS mirror. Do not use the private native Site as a judge's only link.
This walkthrough inspects a completed real agent purchase; it does not execute another transaction.

| Time | Screen action | English narration |
| --- | --- | --- |
| 0:00–0:15 | Open the landing page; run the supplier comparison | “Agents repeat discovery, negotiation and payment handling across suppliers. Economic Machine turns those steps into reusable buying and selling policies.” |
| 0:15–0:30 | Open `/commerce/submission` | “This is an actual Arbitrum Sepolia agent purchase. One snapshot, a 0.01 test-USDC budget, internal research rights. The seller's 20% rule discount turns a 0.0125 ask into the accepted 0.01 offer.” |
| 0:30–0:45 | Change budget to `0.001`; press Replay policy; restore `0.01` | “The same engine rejects an offer above budget. Replay uses the original purchase time and creates no new payment. Routine matching and negotiation use zero model calls.” |
| 0:45–1:00 | Expand Signer & authorization | “The disposable buyer signed an EIP-3009 authorization with an external CLI. We recovered the signature from the actual token call and matched its payload hash to the application's stored submission.” |
| 1:00–1:15 | Open Arbiscan; inspect the same transaction; return | “Circle test USDC transferred exactly 10,000 atoms on Arbitrum Sepolia. Two RPCs confirm the canonical finalized receipt and consumed authorization. This is an existing token contract, not our own deployed escrow.” |
| 1:15–1:35 | Expand Transaction & state, then Delivery & artifact hash | “The runtime is settled: 0.01 spent, zero held. The received JSON carries the same terms hash and immutable data version. Its block contents independently match the public chain.” |
| 1:35–1:50 | Expand Runtime transition record; open recorded console | “Preparation, challenge admission, authorization, settlement report and final reconciliation are linked in the runtime journal. Humans manage API keys, shared spending limits and activity.” |
| 1:50–2:00 | Return to evidence page | “The model interprets intent; the engine executes bounded commerce. GPU and RAM feeds are a planned vertical. This demonstration uses test tokens and recorded HTTP delivery, without claiming atomic delivery, customer adoption or mainnet readiness.” |

If mentioning performance, use the scoped results in [ARBITRUM_SUBMISSION.md](ARBITRUM_SUBMISSION.md):
87.5% fewer warm-path requests versus cached direct code; 99.66% fewer startup-inclusive tokens versus
the measured Qwen loop. Do not claim a higher success rate than cached deterministic code.

Capture both the original accepted policy and one rejected policy. Keep the actual transaction hash,
delivered version and runtime state readable. Never film a key file, API token, password or signing secret.
