# Optional Cloudflare deployment

For the recommended private setup, follow the README's [Host steps](../README.md#host-steps) and [Friend steps](../README.md#friend-steps). This page retains the optional Cloudflare Worker deployment for experienced administrators. The Worker and SQLite-backed Durable Object remain in `cloudflare/`; client tools use the same interface as the Python server. See the README for the shared [troubleshooting table](../README.md#troubleshooting) and [glossary](../README.md#glossary).

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

## Connect the clients

After deployment, use the README's [Friend steps](../README.md#friend-steps) to install the client and run `agentmesh-setup` on each computer. Use your own Worker's full `https://` address, choose `agent-a` on one computer and `agent-b` on the other, and enter only that agent's token when setup asks. Run the registration command setup prints, then follow [Check it works](../README.md#check-it-works).

The Python server initialization and Tailscale sharing steps do not apply to this deployment. A publicly reachable Worker does not require Tailscale; protect it with its agent tokens and, if desired, Cloudflare Access.

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

Run the guided setup and registration described [above](#connect-the-clients) first. Then add the two `AGENTMESH_CF_ACCESS_*` entries to the private settings file that setup created at `~/.config/agentmesh/config.env`, and restart the connection in Claude Code. Keep its owner-only permissions; on POSIX systems these are mode 0600. The setup assistant asks only for the agent name, server address, and agent token, so it does not configure Access. If you rerun setup and replace the file, add the Access entries again.

Access headers are optional and are not needed for the Python/Tailscale backend. A browser dashboard behind Access also needs an appropriate browser-access policy.

## Differences from self-hosting

- Worker data lives in a Cloudflare Durable Object, not a local `data.db`.
- `GET /v1/connect` supports the Worker's WebSocket hibernation API; the Python server returns 404. Long-polling works on both.
- The Worker uses Cloudflare's rate limiter and connecting IP; the Python server uses an in-process limiter and loopback-proxy-aware forwarded IP handling.
- Worker secrets can contain comma-separated `new,old` values during token rotation. Update clients before removing the old token. The Python server supports the same format in its private JSON config, read on server startup.
- Both backends retain events/audit for 7 days, acknowledged messages/patches for 30 days, and accepted/cancelled tasks for 90 days.

Use `scripts/e2e_test.py` against the deployed API with both agent tokens supplied through a private environment. For a no-Cloudflare development check, use `scripts/e2e_local.py` instead.
