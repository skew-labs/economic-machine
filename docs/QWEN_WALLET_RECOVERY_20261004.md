# Qwen restoration and MetaMask deployment preflight

2026-10-04 follow-up to the Amazon release. The owner explicitly selected Qwen
while Bedrock organization access remains unresolved.

The Canadian runtime now selects `qwen`, loads its existing private Kiln
configuration, disables Bedrock and removes the Bedrock bearer credential from
the service process. The Strands implementation remains available for a future
authorized switch; this is an explicit provider selection, not automatic fallback.
`/etc/machine-commerce/assistant-provider.env` records the selected provider.
The historical Amazon release script selects Bedrock explicitly and must not be
rerun to deploy unrelated fixes while this Qwen selection is in effect.

A public HTTPS verification logged in with an unfunded disposable wallet, made
one actual Qwen3 32B request and verified saved history. The response took 1,703 ms
and reported 401 prompt tokens and 63 completion tokens. The login signature was
not a financial authorization. There were no Bedrock calls or chain writes.

The reported deployment error was misleading. On the user's actual MetaMask,
`eth_getTransactionCount(..., 'pending')` returned the JavaScript number `1`.
The previous preflight accepted hexadecimal strings only and combined any invalid
RPC result with insufficient ETH in one error message.

The wallet boundary now normalizes nonnegative, safe integer nonces into exact
hexadecimal strings. It rejects fractional, negative, unsafe, missing and malformed
nonces, and still checks both latest and pending nonce against the reviewed
transaction. Monetary quantities retain their strict hexadecimal requirement.
Invalid RPC values and insufficient balance now produce different messages.

A read-only wallet check is available on the deployment page. After the fix, the
actual wallet showed 0.003706451488319571 ETH and latest/pending nonce 1/1; the two
previously reviewed RPC balances matched that amount. This check requests no
signature and does not prove a deployment occurred. The deployment must still use
a fresh source-bound quote and the owner's MetaMask signature.

Eight focused wallet tests passed on the Canadian host, covering the observed
numeric nonce, malformed values, amount/chain/owner/nonce guards and ambiguous
submission recovery. Actual deployment and token issuance remain separate receipt
checks. Evidence is under `artifacts/console-recovery-20261004/`.
