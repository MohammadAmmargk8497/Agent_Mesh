# Optional Cloudflare deployment

The default [quickstart](../README.md) runs the Python server privately over Tailscale. The original Worker and SQLite-backed Durable Object remain in `cloudflare/` for users who prefer Cloudflare hosting. Existing client configuration and tool names are unchanged.

## Deploy the Worker

Requirements: Node.js 20+, a Cloudflare account, and a domain managed by Cloudflare DNS.

```bash
cd cloudflare
npm install
npx wrangler login
```

Review `wrangler.toml` before deployment. Replace the example deployment's routes with your own hostname, such as `agents.example.com`; do not deploy to somebody else's hostname. The current Worker serves the dashboard only on its configured dashboard hostname in `src/index.ts`, so adapting the dashboard to a different domain requires updating that hostname too. The self-hosted Python server has no hostname restriction.

Create two different random agent tokens, each at least 32 characters, and a separate viewer token if the dashboard is needed. Enter each token at Wrangler's interactive secret prompt, not as a command-line argument:

```bash
openssl rand -hex 32
npx wrangler secret put AGENT_A_TOKEN
openssl rand -hex 32
npx wrangler secret put AGENT_B_TOKEN
openssl rand -hex 32
npx wrangler secret put VIEWER_TOKEN
npm run typecheck
npm run deploy
curl --fail https://agents.example.com/health
```

Store the generated tokens in a password manager. Do not commit or publish them. Share each agent's token through an end-to-end encrypted channel. `workers.dev` is disabled in the supplied configuration; use the custom domain route.

## Optional Cloudflare Access

Access provides a second credential checked at the edge before the Worker:

1. In Zero Trust, create two Access service tokens and securely record their client IDs and secrets.
2. Create a self-hosted Access application for the API hostname with a **Service Auth** policy allowing those service tokens.
3. Add the appropriate service token to each client's private config.

Example client configuration (placeholders only):

```dotenv
AGENTMESH_URL=https://agents.example.com
AGENTMESH_TOKEN=PASTE_THIS_AGENTS_TOKEN
AGENTMESH_AGENT_NAME=agent-a
AGENTMESH_CF_ACCESS_CLIENT_ID=PASTE_ACCESS_CLIENT_ID
AGENTMESH_CF_ACCESS_CLIENT_SECRET=PASTE_ACCESS_CLIENT_SECRET
```

Install and register the client as described in the [main quickstart](../README.md#2-install-the-client-on-both-machines). Access headers are optional and are not needed for the Python/Tailscale backend. A browser dashboard behind Access also needs an appropriate browser-access policy.

## Differences from self-hosting

- Worker data lives in a Cloudflare Durable Object, not a local `data.db`.
- `GET /v1/connect` supports the Worker's WebSocket hibernation API; the Python server returns 404. Long-polling works on both.
- The Worker uses Cloudflare's rate limiter and connecting IP; the Python server uses an in-process limiter and loopback-proxy-aware forwarded IP handling.
- Worker secrets can contain comma-separated `new,old` values during token rotation. Update clients before removing the old token. The Python server supports the same format in its private JSON config, read on server startup.
- Both backends retain events/audit for 7 days, acknowledged messages/patches for 30 days, and accepted/cancelled tasks for 90 days.

Use `scripts/e2e_test.py` against the deployed API with both agent tokens supplied through a private environment. For a no-Cloudflare development check, use `scripts/e2e_local.py` instead.
