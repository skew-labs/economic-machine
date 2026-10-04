# Task payment recovery

A bounded durable worker resumes only existing payment intents and paid jobs. It does not create or capture orders. Cancellation before submission revokes the plan; cancellation during an uncertain capture retains a pending cancellation. A completed payment requires refund reconciliation and never becomes an unpaid cancellation.

Owner-only full refunds persist the idempotency key before POST. Lost responses remain unknown until the exact capture ID, USD amount and refund state are read back. Refund requests are not retried automatically. Duplicate verified webhooks cannot grant another delivery; external refunds revoke access. Previously downloaded files cannot be revoked.

Configure `ENGINE_PAYPAL_SANDBOX_FILE` as a private file and `ENGINE_PAYPAL_WEBHOOK_ID` for the registered sandbox merchant. `/api/task-webhooks/paypal` verifies each event through PayPal before resolving an existing tenant order, then reads canonical payment state. Order-linked capture events are supported. Events without `supplementary_data.related_ids.order_id` fail closed; reconciliation still runs periodically. No live PayPal environment is admitted.

Tests cover concurrent capture/cancellation, lost refund responses, readback mismatch and post-refund access with fixture adapters. Live sandbox integration requires a configured test seller, buyer and webhook registration.
