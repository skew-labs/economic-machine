# SKEW task tools over MCP

Endpoint: `https://skew.deals/commerce/api/engine/mcp` (reverse-proxied hosted deployment) or `http://127.0.0.1:8800/api/engine/mcp` when self-hosted. This is a stateless JSON response form of Streamable HTTP, protocol 2025-11-25. GET returns 405 because no unsolicited SSE is provided. Initialization negotiates the version; subsequent requests require the protocol header. Notifications receive empty 202 responses.

Create a scoped key in the owner console. Connect an MCP client using `Authorization: Bearer <private scoped key>`, `Accept: application/json, text/event-stream`, `Content-Type: application/json`, and after initialization `MCP-Protocol-Version: 2025-11-25`. Credentials stay in the client secret store, not URLs. This is pre-provisioned bearer authentication; OAuth discovery is not implemented. Clients that require OAuth-only onboarding are not supported.

Seven tools share the existing workspace: create_task, get_confirmed_preferences, compare_plans, request_purchase_approval, get_task_status, get_deliverable and request_cancel. Hosted keys need engine:read to connect, engine:write to create tasks/proposals, and data:read for delivery. No tool captures, signs, approves or refunds. Purchase and cancellation tools point to the owner console. Missing services return no eligible offers, not fabricated plans. Preferences returned are confirmed and unexpired.

Every tool resolves the authenticated workspace. Request IDs replay saved drafts; unknown IDs from another workspace fail. No arbitrary URLs, shell, wallet keys, MCP tool discovery from untrusted servers or third-party credentials are accepted. Origin, body size and caller rate limits apply. Self-hosted MCP currently accepts its local owner token; dedicated self-hosted per-agent keys remain restricted to agent-run endpoints.

Specification: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports

Remote tests exercise initialization, discovery, tool calls, notifications, protocol errors, missing auth, hostile Origin, scope rejection, restart resumption, cross-workspace reads and payment boundaries. These are actual HTTP protocol tests against the app with fixture payment adapters; they do not establish Alexa runtime registration.
