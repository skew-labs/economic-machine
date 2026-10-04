# Operating the runtime

Start with [self-hosting](SELF_HOSTING.md). A source checkout starts an owner-controlled
loopback service; it is not an automatically provisioned public commerce deployment.

## Configure explicit authority

Keep credentials in private environment files and use provider-side permissions appropriate to
readers or execution adapters. A reader should not have withdrawal authority. Configure the
merchant registry, accepted tokens, networks and recipients before admitting paid requests.
Use TLS, secure owner sessions and scoped API keys for any exposed service.

Venue execution stays disabled until the operator enables its transmission gate. That gate
does not replace exact per-order approval. Contracts and transaction drafts also require a
separately authorized owner signature. See [security boundaries](../SECURITY.md).

## Recover the original operation

Persist the submission attempt before contacting an external service. On timeout, retain its
reservation and reconcile the same order ID or nonce. Verify finalized receipts and actual
balances; a transaction hash or a provider's success response alone is insufficient.

Payment confirmation, access entitlement and delivered content are separate states. For a paid
request with missing delivery, resume delivery or use the documented dispute/recovery path;
do not charge the customer again or claim a refund without payment evidence.

## Maintain private state

Back up databases and journals with encryption to an independently controlled destination.
Test restore into an isolated environment, including pending reservations and recovery jobs.
Keep backups, secrets, private salts and operator configuration outside Git.

Record the deployed source revision, dependency and native-library hashes, contract addresses
and configured network. Rebuild and revalidate any changed component; historical evidence is
not validation of a later release. Observe queue limits, rate limits, worker restarts, stale
feeds and upstream failures before widening a deployment.

[Fuel API](FUEL.md) · [Mining operations](SOLUTION_OPERATIONS.md) · [Commerce](SERVICE_COMMERCE.md)
