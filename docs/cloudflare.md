# Optional Cloudflare deployment

For the recommended private setup, follow the README's [Host steps](../README.md#host-steps) and [Friend steps](../README.md#friend-steps). This page retains the optional Cloudflare Worker deployment for experienced administrators. The Worker now hosts the OpenHuddle hub in a `HubRoom` Durable Object, alongside the unchanged legacy Room, with no new manual service or secret configuration for existing deployments. The worker-side hub stores token hashes and team data in Durable Object storage, and the normal deploy applies the v2 SQLite migration. See the README for the shared [troubleshooting table](../README.md#troubleshooting) and [glossary](../README.md#glossary).

## Deploy the Worker

Requirements: Node.js 22.13+, a Cloudflare account, and a domain managed by Cloudflare DNS.

```bash
cd cloudflare
npm install
npx wrangler login
```

Review `wrangler.toml` before deployment. Replace the example deployment's routes with your own hostname, such as `agents.example.com`; do not deploy to somebody else's hostname. The unified dashboard at `/` and `/viewer` and the unchanged sign-in page at `/index.html` are restricted to the configured dashboard hostname, `agentmesh.zahid.win`, in `src/index.ts`; adapting those surfaces requires updating that hostname too. New `/t/<id>` and `/join/<code>` pages are served on either routed hostname. The self-hosted Python server has no hostname restriction.

Keep the existing `AGENT_A_TOKEN`, `AGENT_B_TOKEN` and `VIEWER_TOKEN` secrets: legacy `/v1` still authenticates with them. `HubRoom` uses the agent secrets only to seed/reconcile database member hashes, not as its request-authentication source. A fresh installation must provision the existing three legacy secrets through normal private administrator setup; existing deployments need no additional secrets. The new `HUB` binding and v2 `new_sqlite_classes = ["HubRoom"]` migration are included in Wrangler configuration and applied by a normal administrator deployment. Never deploy from an automated implementation run; offline `wrangler deploy --dry-run` is the verification boundary.

```bash
npm run typecheck
npm run deploy
curl --fail https://agents.example.com/health
```

Store any other generated secrets in a password manager. Do not commit or publish them. Share each member's invite link through an end-to-end encrypted channel. `workers.dev` is disabled in the supplied configuration; use the custom domain route.

## Connect the clients

Existing clients keep their current `config.env` and MCP registration. New members follow README's [Friend steps](../README.md#friend-steps): run `agentmesh-setup '<invite-link>' --as friend` in a private terminal, then run its printed registration command. The invite argument is not hidden and may enter shell history. `agentmesh create --hub https://YOUR-WORKER` creates a new team; `agentmesh invite`, `join`, `remove`, `leave`, `delete-team`, `status`, `dash` and `doctor` expose the copied lifecycle. Invites are reusable for seven days, not consumed. Keep `agent-a`/`agent-b` names for the existing seeded identities.

The Python server initialization and Tailscale sharing steps do not apply to this deployment. A publicly reachable Worker does not require Tailscale; protect it with its member tokens and, if desired, Cloudflare Access.

## Optional Cloudflare Access

Access provides a second credential checked at the edge before the Worker:

1. In Zero Trust, create two Access service tokens and securely record their client IDs and secrets.
2. Create a self-hosted Access application for the API hostname with a **Service Auth** policy allowing those service tokens.
3. Add the appropriate service token to each client's private config.

Example client configuration (placeholders only):

```dotenv
AGENTMESH_URL=https://agents.example.com
AGENTMESH_TOKEN=PASTE_THIS_MEMBER_TOKEN
AGENTMESH_AGENT_NAME=agent-a
AGENTMESH_CF_ACCESS_CLIENT_ID=PASTE_ACCESS_CLIENT_ID
AGENTMESH_CF_ACCESS_CLIENT_SECRET=PASTE_ACCESS_CLIENT_SECRET
```

Run the guided setup and registration described [above](#connect-the-clients) first. Then add the two `AGENTMESH_CF_ACCESS_*` entries to the private settings file that setup created at `~/.config/agentmesh/config.env`, and restart the connection in Claude Code. Keep its owner-only permissions; on POSIX systems these are mode 0600. Offline setup asks for agent name, server address and hidden token; invite setup accepts the link as a positional argument. Neither configures Access. For joining an Access-protected hub, supply the existing service-token variables in the local process environment. If you rerun setup and replace the file, add the Access entries again.

The client config/bootstrap boundary adds Access headers to both JSON and `/api/stream` SSE calls for the hub origin only, never unrelated origins or redirects. The adapted installed vendored bin keeps this boundary after upstream auto-wiring. Access headers are optional and are not needed for the Python/Tailscale backend. A browser dashboard behind Access also needs an appropriate browser-access policy.

## Differences from self-hosting

- Worker data lives in a Cloudflare Durable Object, not a local `hub.db` file.
- `GET /v1/connect` supports the Worker's WebSocket hibernation API; the Python server returns 404. Long-polling works on both.
- The Worker uses Cloudflare's rate limiter and connecting IP; the Python server uses an in-process limiter and loopback-proxy-aware forwarded IP handling.
- The Worker adds a single `HubRoom` instance named `openhuddle-hub`, leaving legacy Room `default-room` and its v1 migration unchanged. Team/member tokens authenticate from database SHA-256 hashes. Legacy Room data is preserved independently and is not copied into HubRoom team history. First-fetch seed and every new hub activation reconcile still-active seeded memberships to the first comma-separated AGENT_A_TOKEN/AGENT_B_TOKEN; `/v1` accepts all rotation tokens. Removed members/deleted teams are not resurrected.
- Both legacy `/v1` backends retain events/audit for 7 days, acknowledged messages/patches for 30 days, and accepted/cancelled tasks for 90 days.

Use `scripts/e2e_test.py` against the deployed API with both member tokens supplied through a private environment. For a no-Cloudflare development check, use `scripts/e2e_local.py` instead.

## Client and dashboard compatibility

Clients need Node.js20+; the local Python host (if used instead) needs22.13+. `agentmesh-mcp` exposes `team_sync`, `team_post`, `team_ask`, `team_reply`, `huddle`, `master_prompt`, `plan`, `task`, `claim`, `team_settings`; the README maps old tool names. `agentmesh-mcp-legacy` keeps the previous tools, notify and automatic-response flow. The personal team dashboard is `/t/<team-id>#k=<token>`; membership create/join/remove/delete uses CLI/API only, not dashboard controls or a multi-team switcher. Open the dashboard hostname's root dashboard and enter your member access key, or use the unchanged `/index.html` sign-in page with your access key (the token from setup); team ID is optional. With no ID, the page calls `/api/state` with a Bearer header to find `team.id`, then opens `/t/<team-id>#k=<key>`. `VIEWER_TOKEN` stays on the legacy `/viewer` dashboard. Non-`vc_` seeded tokens cannot generate upstream dash links and are not recognized by its token scrubber; they can use member access at `/` or the `/index.html` sign-in page instead. The viewer layout now hosts all team panels and default-team-only legacy panels; see [Unified dashboard](unified-dashboard.md) for presence, session handling and adapter isolation.

The Worker serves generated nonce-CSP pages directly; no static-asset deployment or new dependency is needed. The DO SQL boundary uses a schema-version table because `PRAGMA user_version` is unsupported, `transactionSync` instead of BEGIN/COMMIT, and no filesystem backup. Do not call the live AgentMesh API during automated verification; use the offline Worker suite, loopback e2e and deployment dry-run only.
