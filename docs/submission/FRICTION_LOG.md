# Product and integration feedback — 2026-10-04

- Bedrock API-key onboarding can succeed while the organization's SCP denies `bedrock:CallWithBearerToken`. The runtime now reports an organization-level blocker without leaking account identifiers and does not retry or silently select Qwen.
- Strands 1.57.2 rejects supplying both `boto_session` and `region_name`. The integration now assigns the region to the session and verifies a real SDK tool loop using stubbed AWS responses.
- An old system boto3 could satisfy loose transitive constraints but did not expose Converse. The Amazon optional dependency now requires boto3 >=1.40, and the installed versions are recorded.
- Initial isolated test commands missed the scripts import path and compiled ABI location. Evidence retains those failures and the corrected passing checks.
- The standalone commerce API omitted assistant asset routes even though the public portal served them. Focused browser-asset verification identified and fixed this gap.
- PayPal sandbox provisioning is an account dependency, not something an adapter or a fake approval redirect proves. The public UI keeps payment admission closed without the merchant configuration.
- Browser dictation is an optional convenience and does not itself demonstrate Alexa+ runtime integration. Reviewable typed input remains available.

- The new isolated SDK environment did not inherit the old editable source path at service startup. A deployment check caught the failed runtime; explicit PYTHONPATH restored it. Release orchestration now preflights imports and waits for health before reporting success.
- A delayed conversation-history response could erase a newly opened plan card during login initialization. The console now hydrates history only into an untouched, empty conversation. Public-domain execution and a delayed-history browser regression verify the fix.
- systemd applies drop-ins lexicographically: an earlier Bedrock file was overridden by the old assistant environment. The final `zz-amazon.conf` wins and the live authenticated provider endpoint confirms Bedrock with no fallback.
