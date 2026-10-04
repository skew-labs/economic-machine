# The console is the engine's control surface

Economic Machine is an open-source console for connecting your own APIs and running multiple agents
under shared budgets and rules. Engine and console share the same owner workspace, SQLite database,
connections, typed policy IDs, run IDs, order IDs and integrity-checked event journal. Hosted wallet login
isolates each owner; self-hosted loopback installations use a local owner token and private databases.

## Execution contract

1. Connect an exchange, wallet, data endpoint or AI model catalog. Credentials are references to the
   owner's process environment; the console does not receive and persist raw exchange keys.
2. Define immutable shared rules: allowed connections, allowed operations, permitted venue policies,
   gross USDT turnover limit, per-order limit, maximum simultaneous tasks and expiry.
3. Register named agents with a role and a narrower subset of those connections and operations.
4. Issue an agent-bound key. It can run only that agent's tasks and read its own metadata/results.
5. Tasks reuse the existing engine: `SYNC_CONNECTION`, `NATIVE_CANDIDATE`, `PLAN_VENUE_ORDER`.
6. Order plans reserve the shared envelope before venue reads and attach the actual venue order to
   the agent run in one transaction. The owner's existing Execution view reviews that exact plan.
7. Approval and dispatch recheck agent, shared-policy and connection authority. Every financial order
   still needs owner approval; creating an agent/key grants no signer or live-transmission authority.
8. Read actual venue outcomes and reconcile. Terminal gross turnover is charged once. An UNKNOWN
   outcome retains the hold. Pausing a policy cannot erase the outstanding reservation.

Sync and native numerical candidates do not consume gross venue turnover. USD reported AI usage,
USDT gross venue turnover and token-specific external service payments remain separate units. This
is a common control plane, not a cross-asset cash balance. Fees remain in the venue's existing fee
reserve; gross turnover is not profit/loss or net cash spending. In a SELL, favorable price improvement
can exceed the planned turnover: account for observed proceeds and mark the team BREACHED rather
than hiding the observation. Native inputs remain caller-supplied, not attested account facts.

## API

Owner routes, relative to `/api/engine`:

- `GET /control`: policies, named agents, recent runs, typed turnover counters.
- `POST /control-policies`: immutable shared rules.
- `POST /control-policies/{id}/pause`: stop future authority, retain unresolved holds.
- `POST /agents`: assign name/role, policy and narrower operations/connections.
- `POST /agents/{id}/pause`: stop that agent without stopping another agent.
- `POST /agents/{id}/runs/{run}/withdraw`: release only an untransmitted plan.

A bound agent key may use only `GET /agents/{id}`, `POST /agents/{id}/runs` and
`GET /agents/{id}/runs/{run}` for its own ID. A stable request ID with identical input returns the
same result; reuse with changed input or another agent fails. Hosted `POST /api/keys` uses
`scopes: ["agents:run"]` and `engine_agent_id`. Optional `engine:read` admits workspace reads but
never order approval. Combining `agents:run` with `engine:write`/other mutation scopes is rejected.
Self-hosted owner key issuance uses `POST /api/engine/agents/{id}/keys`; only key hashes are stored.
Keys expire/revoke independently, with expiry capped at the shared policy lifetime.

Example bound-key task:

```json
{
  "request_id": "watch-sync-001",
  "operation": "SYNC_CONNECTION",
  "connection_id": "connection-owner-defined-id",
  "payload": {}
}
```

Create policies and agents from **Agents & limits**. An agent's API integration disclosure shows its
real URL and IDs. Create key returns the secret once. Execution history joins name, connection,
shared policy, hold, final turnover, reason and linked order. **Review order** opens the existing
exact-plan approval view; this is the same order, not a second execution implementation.

## Failure and restart semantics

Run admission serializes reservation under `BEGIN IMMEDIATE`. Connection I/O is outside the SQLite
transaction. A 120-second task lease bounds preparation. Expired unlinked work becomes INTERRUPTED,
and a late venue-plan commit is rejected. No automatic replay of a failed financial instruction.
Durable schedulers and reconciliation continue to use the existing runtime workers. Named agent tasks
are invoked by API callers or the console; this change does not create an autonomous LLM agent loop.
Owner-selected scheduled connection synchronization remains available in Connections.

Legacy trusted `engine:write` tool keys retain their previous scope. Use **agent-bound** keys for
untrusted strategy processes that must share a team envelope. Older manually compiled orders stay
under their original venue policy; they are not silently reassigned to a new team's budget. Generic
per-provider AI inference/spend admission, resume/edit policies and further venue adapters remain
unimplemented. AI connectors currently list model catalogs and accept reported usage; they do not
invoke models. Hosted live trading stays disabled. External x402 purchases keep existing mandates;
own exchange trades do not pass through commerce payment.

## Tests

```sh
PYTHONPATH=src:tests .venv/bin/python -m unittest -v \
  test_agent_control test_engine_execution test_access test_engine_workspace test_engine_portal
```

Agent control tests exercise simultaneous cross-agent reservations, separate child venue policies,
policy/agent narrowing, exact idempotency, bound-key isolation/revocation, approval then pause/expiry,
late-work recovery, withdrawal boundaries, UNKNOWN restart recovery, actual native C++ evaluation,
shared task concurrency, SELL turnover overshoot and identical expired-order status across console views. Venue I/O uses isolated test adapters. No real
exchange order, signature or chain transaction is part of these tests.

For browser verification, `tests/agent_console_fixture.py` starts a disposable loopback server at 8803,
forwarded to 18803. It uses a known test-only owner token and labels the UI **Disposable UI fixture**.
There are no live vendor keys, network trades or real holdings. Stop the process after inspection.
