# Security

Economic Machine is an early open-source release. No independent production security audit is claimed.
The standalone Engine binds to loopback and requires a private owner token for every account API.
Run it in an environment you control, keep provider credentials in that environment, and grant readers
read-only provider permissions. A local owner token is not public multi-tenant authorization.

Do not include credentials, seeds, private keys, runtime databases or customer balances in issues.
Report reproducible issues with synthetic state and redacted logs. For a vulnerability requiring private
details, use the repository's private vulnerability reporting channel if enabled; otherwise open a minimal
issue requesting a private channel without disclosing the exploit or sensitive data.

Compiled intents and capital locks do not grant financial signing authority. External authorization,
chain outcome and delivered data must be reconciled separately. An ambiguous submitted payment remains
encumbered and must not be blindly retried or marked refunded. The self-hosted reader layer has no
wallet signer, withdrawal endpoint, exchange-order sender or automatic model inference.

Provider URLs are bounded HTTPS reads with DNS pinning and no redirects. This does not establish the
quality or truth of a data source. Usage reports are local ledger entries, not provider invoices.
