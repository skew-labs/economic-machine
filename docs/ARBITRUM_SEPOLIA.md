# Public Arbitrum Sepolia x402 transaction

This is a public-testnet purchase by a dedicated buyer agent from a dedicated seller agent. It uses Circle's
test USDC and a separately isolated seller gas payer. It is not a customer purchase, mainnet payment,
contract deployment, external marketplace sale, or production launch.

The transaction is successful and finalized according to both the official Arbitrum RPC and dRPC.
The runtime is `SETTLED`: 10,000 atoms spent, zero reserved. Historical balances, the consumed authorization
nonce, exact transfer logs and delivered block fields were independently checked. The proof and audit are
saved; explorer `Success` alone was not used to release the hold.

The [joined purchase viewer](https://machine.148-113-153-116.nip.io/commerce/submission) now ties the
runtime's authorization payload hash to the signature recovered from the actual token transaction,
and joins buyer conditions, seller policy, mandate, received artifact and finalized reconciliation.
Current receipt/signature/block readbacks are separated from the preserved earlier historical-balance
audit. A public RPC no longer serves the required old account state; historical balances are not labelled
as freshly rechecked. See [submission scope](ARBITRUM_SUBMISSION.md).

## Transaction

| Field | Observed value |
| --- | --- |
| Network | Arbitrum Sepolia, `eip155:421614` |
| Asset | Circle test USDC, `0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d` |
| Buyer | `0x24d0B9Bc844754Dd1b22f215EBb06627f7314E79` |
| Seller / gas payer | `0xd26491D35Ed8725Ef2bd1E3DB1A1F8959ed31e97` |
| Negotiated price | 0.0125 ask → 20% rule discount → 0.01 test USDC |
| Payment | 10,000 atoms, one public finalized-block snapshot, internal research use |
| Transaction | [0x3aa1cbbb…59b11758](https://sepolia.arbiscan.io/tx/0x3aa1cbbb04e18c0a1d07b3c9cc92b255bc7a3bdb6b62080fa1fb933659b11758) |
| Receipt block | 314873781 |
| Buyer test USDC | 20 → 19.99 |
| Seller test USDC | 0 → 0.01 |
| Seller gas cost | 0.000008592206624 test ETH; buyer gas cost zero |
| Delivered version | `block-314868529` |
| Final runtime state | `SETTLED`, spent 10,000 atoms, reserved 0 |

Circle's [official token registry](https://developers.circle.com/stablecoins/usdc-contract-addresses)
identifies this token; its name `USD Coin`, version `2`, six decimals and EIP-712 domain separator were also
read from the actual contract. Test tokens have no financial value. The chain ID and primary endpoint follow
[Arbitrum's RPC documentation](https://docs.arbitrum.io/arbitrum-essentials/reference/node-providers).

## Actual execution path

1. Provision two isolated test operator workspaces and scoped seller/buyer API keys on the authorized Canada host.
2. Capture a real finalized block through the public RPC and register its immutable version as the seller's resource.
3. Seller agent registers its selling rule; buyer agent registers price, quantity, age, purpose and license limits.
   The real HTTP demand response returns the compatible agreement; no LLM inference is used.
4. An owner-created 0.01 test-USDC mandate admits this one resource. The buyer's key requests a payment;
   the engine verifies the HTTPS merchant's x402 v2 challenge and the actual token contract/domain/balance.
5. A separate operator CLI signs the disposable buyer's exact EIP-712 authorization. The buyer runtime has
   no wallet key. The CLI commits its transmission-attempt state before submitting the signed header once.
6. The independent test seller process validates its one approved order and externally supplied buyer signature,
   simulates the actual `transferWithAuthorization` call, and pays test ETH for that transaction.
   The signed transaction hash is committed before broadcasting. No ambiguous outcome creates a replacement transfer.
7. The HTTPS seller returns `PAYMENT-RESPONSE` plus the negotiated terms hash, data version and public block snapshot.
8. The unchanged Economic Machine runtime checks authorization/transfer logs, the canonical receipt and the actual
   `finalized` tag. Before finality, its hold remains reserved. After finality it records spent capital and zero hold.
9. An independent read-only CLI repeats receipt/log/nonce/historical balance/data checks through two public RPCs.

Buyer signing, seller gas signing and runtime custody are separate processes. The disposable test keys are stored
only in root-protected server files. The seller service receives only its own key through a systemd credential;
the buyer runtime receives operator password hashes and its resource registry, without either wallet key.
No secrets or plaintext payment signatures are written to source, evidence or chat.

## Evidence and verification

Evidence lives in `artifacts/arbitrum-sepolia/` on the authorized server. Only small, nonsensitive files are copied
to the Mac; all tests, RPC calls, EVM execution and audits run remotely.

- `preflight.json`: actual chain/token bindings, initial balances and registered resource.
- `agreement.json`: real HTTP policies, negotiation trace, scoped key metadata and mandate.
- `submission.json`: transmitted order, authorization hash, tx hash and recorded delivery; no signature plaintext.
- `reconciliation.json`: latest runtime observation, shared capital state and replay check.
- `proof.json`: created only after finalized payment, exact payer/recipient deltas, zero hold and delivered data checks.
- `receipt.json`: actual token receipt and logs, without transaction input/signature material.
- `independent-audit.json`: two RPC readbacks of receipt/logs, nonce, historical balances and delivered block contents.
- `boundary-checks.json`: second purchase rejected for exhausted shared capital; buyer key denied mandate administration.
- `verification.json`: source/evidence hashes, service health, targeted test counts and synchronization record.

Eighteen new remote tests pass: eleven merchant tests and seven operator CLI tests. They cover startup domain
checks, fixed testnet/amount/recipient admission, wrong signatures,
expiry, unsigned challenge, exact delivery envelope, concurrent/restarted submit, ambiguous broadcast,
receipt mismatch, owner HTTP lifecycle and refusal to replace a transmitted payment. Existing unchanged
contracts/core/Qwen workloads are not rebuilt or rerun.

The read-only auditor rejects six deliberately corrupted records: amount, token, delivery hash, capital hold,
rehashed false block contents and a substituted recipient. The latter two require actual RPC readback; an
unavailable RPC is not accepted as a successful corruption rejection. Both actual RPC providers report a
canonical successful receipt below their finalized head and agree on historical balances and data contents.
PublicNode could read receipts but could not serve the required historical state, so archival verification
uses dRPC. The existing transaction was retained throughout; no payment was retransmitted.

Reproduce the read-only check on an authorized remote host:

```sh
.venv/bin/python scripts/audit_sepolia_proof.py artifacts/arbitrum-sepolia/proof.json \
  --negative-checks --output artifacts/arbitrum-sepolia/independent-audit.json
```

The auditor needs no private key or account and contains no transaction submission call. An operator must
explicitly authorize any new payment; `sepolia_demo.py execute` refuses to repeat an attempted submission.
`renew` may cancel and replace only a payment the runtime confirms was never transmitted. The initial expired
unsigned preparation is retained as cancelled evidence, not counted as a payment.

## Limits

The merchant is a deliberately narrow test seller: one approved request, one payer, fixed token/network,
0.01 test-USDC maximum, capped gas, registered HTTPS URL and one broadcast commitment. It is not a general
merchant/facilitator service or a contract-safe arbitrary signing tool. Its local `broadcasts` counter records
a committed broadcast attempt; the independent receipt proves that the committed transaction actually executed.

Arbitrum `finalized` is checked through approved RPC providers. This is not an independently verified parent-chain
proof. x402 payment is not atomic with delivery and EIP-3009 does not bind legal licenses or data quality. The
demo's data check is limited to the delivered public block fields. The engine handles paid-but-missing-delivery
as a dispute state rather than inventing a refund. No customer key, real-dollar funds or mainnet asset was used.
The original development console and service remain in place; the separate buyer runtime is private on port 4261.
