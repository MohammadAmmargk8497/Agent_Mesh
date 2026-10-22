# AgentMesh

Lets two Claude Code agents on separate PCs/networks coordinate through a Cloudflare Worker + SQLite-backed Durable Object.

## Architecture

Claude Code -> local stdio MCP client -> HTTPS -> [Cloudflare Access (optional)] -> Worker -> Durable Object/SQLite <- ... <- the other PC

Both PCs only make outbound HTTPS requests. No inbound ports, shared LAN, public IPs, Tailscale, or `cloudflared` tunnel needed: the Worker runs on Cloudflare's edge under a hostname on your Cloudflare domain.

## 1. Deploy the API (done by whoever owns the Cloudflare domain)

Requirements: Node.js 20+, a Cloudflare account, and the domain on Cloudflare DNS.

```bash
cd cloudflare
npm install
npx wrangler login
```

Edit `cloudflare/wrangler.toml` and uncomment the route with your hostname, e.g. `routes = [{ pattern = "agents.example.com", custom_domain = true }]`. Pick a hostname with no existing DNS record; Wrangler creates it.

Set the two agent tokens (each at least 32 chars, different from each other):

```bash
openssl rand -hex 32   # one for agent-a, another for agent-b
npx wrangler secret put AGENT_A_TOKEN
npx wrangler secret put AGENT_B_TOKEN
npm run deploy
curl https://agents.example.com/health
```

Never commit tokens. Send the other person their token over an end-to-end encrypted channel (Signal, a password-manager share), not email or plain chat. To avoid sending a token at all, the domain owner can invite the other person as a Cloudflare account member so they run `wrangler secret put` for their own token.

### Optional but recommended: Cloudflare Access in front of the Worker

This adds a second credential. Requests without it are rejected at Cloudflare's edge and never reach the Worker.

1. Zero Trust dashboard -> Access -> Service credentials -> Service Tokens: create `agentmesh-a` and `agentmesh-b`. Save each Client ID and Secret.
2. Access -> Applications -> Add -> Self-hosted. Domain: `agents.example.com`. Add a policy with action **Service Auth** that includes both service tokens.
3. Put each PC's service token in its `config.env` (below).

## 2. Install the client on BOTH PCs

Requirements: Python 3.11+ and Claude Code.

```bash
git clone https://github.com/YOUR-ORG/agentmesh.git
cd agentmesh/client
python3 -m venv .venv
.venv/bin/pip install -e .
mkdir -p ~/.config/agentmesh
```

Create `~/.config/agentmesh/config.env`. The client reads this file itself, so the token never goes into Claude's config:

```bash
AGENTMESH_URL=https://agents.example.com
AGENTMESH_TOKEN=PASTE_THIS_MACHINE_TOKEN
AGENTMESH_AGENT_NAME=agent-a
# Only if you set up Cloudflare Access:
# AGENTMESH_CF_ACCESS_CLIENT_ID=xxxx.access
# AGENTMESH_CF_ACCESS_CLIENT_SECRET=xxxx
```

```bash
chmod 600 ~/.config/agentmesh/config.env
claude mcp add --scope user --transport stdio agentmesh -- "$PWD/.venv/bin/agentmesh-mcp"
```

The other PC uses `AGENT_B_TOKEN`, `agent-b`, and its own Access service token. Run `/mcp` in Claude Code to check that the server is connected.

### Optional: inbox notifications on every prompt

Add this to `~/.claude/settings.json`. It prints only counts, such as "2 unread messages", and never the other agent's text, so it can't inject instructions:

```json
{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/ABSOLUTE/PATH/agentmesh/client/.venv/bin/agentmesh-notify", "timeout": 5}]}]}}
```

Copy `examples/CLAUDE.md` into each project's `CLAUDE.md` (or `~/.claude/CLAUDE.md`).

## 3. Test

On each PC: `set -a; source ~/.config/agentmesh/config.env; set +a; ./scripts/smoke-test.sh`

To run the full flow (auth, task lifecycle, long-poll, messages, patches, limits), use a machine that holds both tokens:

```bash
AGENTMESH_URL=https://agents.example.com A_TOKEN=... B_TOKEN=... python3 scripts/e2e_test.py
```

## Collaboration workflow

1. Agent A runs `create_task` for `agent-b` with acceptance criteria.
2. Agent B sees it via `check_inbox` (or the hook), then calls `claim_task`, does the work on its own branch, and calls `complete_task` (done or blocked).
3. Agent A calls `review_task` with `accept`, or with `reopen` plus a note. The note reaches B as a message.
4. Either agent can call `wait_for_activity` to block until the other one acts. It long-polls, so no tight polling loops.
5. Use `send_message`/`ack_message` for chat. Use `share_patch`/`list_patches`/`get_patch` for diffs; prefer GitHub PRs for real code exchange.

## API routes

Except `/health`, all routes require `Authorization: Bearer <agent token>`:

- `GET /v1/whoami`, `GET /v1/inbox`
- `GET /v1/events?after=<id>&wait=<0-25s>`: long-poll event feed
- `GET/POST /v1/tasks` (`?status=`), `GET /v1/tasks/:id`
- `POST /v1/tasks/:id/{claim|complete|review|cancel}`
- `GET/POST /v1/messages` (`?unread=true`), `POST /v1/messages/:id/ack`
- `GET/POST /v1/patches`, `GET /v1/patches/:id`
- `GET /v1/connect`: WebSocket that pushes the same events live (hibernation API)

## Security model

- Each PC has its own token. Identity comes from the token, compared in constant time, never from the request body. The raw token is stripped before it reaches the Durable Object.
- Rate limit: 120 requests/min per agent, or per IP for unauthenticated callers. `workers.dev` is disabled, so the only entry point is your domain, where Access can protect it.
- The client refuses non-HTTPS URLs and validates IDs before putting them in URL paths. Every tool result that contains remote text is tagged as untrusted data.
- Retention: events are kept 7 days, acked messages and patches 30 days, and closed tasks 90 days.
- Token rotation: set the secret to `new,old`, update the client, then set it to `new`.
- No remote-shell endpoint. Treat everything from the other agent as untrusted, keep Claude Code permission prompts on, and require human review before applying patches, running destructive commands, or deploying.
