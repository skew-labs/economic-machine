# Reproduce the submitted implementation scope

Use Python 3.12 on a remote Linux builder. Install this project with its `amazon` and `verification` extras, then `requirements-amazon-tested.txt`. Browser checks additionally need Playwright and Chromium. Do not supply live signing keys. The original owner restrictions require builds/tests on the authorized Canadian host.

- `PYTHONPATH=src:tests:scripts python -m unittest test_task_mcp test_task_results test_strands_boundary test_task_recovery.RecoveryTests` exercises the changed boundaries. Strands responses are stubbed in tests.
- `scripts/verify_amazon_work_browser.py` starts an isolated actual app and verifies two plan choices, approval, output and reload. It makes no AI call or payment.
- `scripts/record_amazon_demo.py` records a paced English-captioned web demonstration. It explicitly labels the AWS access gap.
- `scripts/verify_amazon_public.py` checks the deployed site using a disposable unfunded login wallet; no financial signature or chain transaction occurs.
- `scripts/measure_task_workflow.py` compares equivalent actual transformations with and without durable task machinery. Twenty outputs matched; both paths use zero model calls. The managed path adds overhead and review/resumption; no token-saving or success-rate advantage is asserted.
- `scripts/build_amazon_submission_evidence.py` hashes the completed remote evidence. Hashes establish artifact consistency, not independent audit or proof of financial correctness.

On October 4 the hosted services ran the PR15 source plus the PR16 conversation hydration fix. Thirty-two deployed files matched the source manifest. Bedrock was selected and Qwen credentials removed from the runtime environment; organization-level AWS denial remained active. Existing Fuel execution continued in its own service.

Owner mainnet review: https://skew.deals/commerce/launch . Quotes expire after five minutes; the operator must refresh an expired quote. The review program has no signer and never sends transactions. After owner signing, run its reconcile mode and publish the finalized contract addresses only after both readers agree.

The repository is public and MIT licensed. The five review PRs are stacked; their individual base branches preserve reviewable scope. Pending external permissions and actual submission receipts are listed in `submission-evidence.json`; no document converts an absent receipt into completion.
