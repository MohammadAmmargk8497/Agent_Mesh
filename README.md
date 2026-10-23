# AgentMesh

Let two Claude Code agents coordinate across machines: durable tasks, messages, text patches, and an inbox. Run the Python server on your own machine and share it privately over Tailscale. No Cloudflare account, domain, or public listening port is required.

```text
Claude Code -> local stdio MCP client -> Tailscale HTTPS -> Python server -> SQLite
Claude Code -> local stdio MCP client ------------------^
```

The existing Cloudflare Worker remains an [optional deployment](docs/cloudflare.md). Both backends use the same client tools and HTTP API; WebSockets are Worker-only.

## 1. Host the server

Requirements: Python 3.11+, a checkout of this repository, and Tailscale installed and connected on the host and both client machines. Share the host with your collaborator or add them to your tailnet, and grant their device access to the host's HTTPS port 8443 in your Tailscale policy. Keep that access limited to the intended collaborators.

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./server
.venv/bin/agentmesh-server init
.venv/bin/agentmesh-server serve
```

Keep `serve` running in this terminal, or run it under your usual process supervisor. It listens on `127.0.0.1:8787` by default. In a second terminal:

```bash
curl --fail http://127.0.0.1:8787/health
tailscale serve --bg --https=8443 localhost:8787
```

Tailscale prints an address such as `https://host.your-tailnet.ts.net:8443`. Use that exact address on both clients. Port 8443 deliberately avoids replacing an existing Tailscale Serve service on port 443. HTTPS certificates must be enabled for your tailnet; if Serve reports that setup is needed, follow its instructions. See the [official Tailscale Serve reference](https://tailscale.com/docs/reference/tailscale-cli/serve).

No router port forwarding is needed. Stop sharing **only this endpoint** with:

```bash
tailscale serve --https=8443 off
```

`init` creates `~/.config/agentmesh-server/config` with independent `AGENT_A_TOKEN`, `AGENT_B_TOKEN`, and `VIEWER_TOKEN` values, and prints client configuration snippets. The file is JSON; do not shell-source it. The directory is mode 0700 and the config and SQLite `data.db` files are mode 0600. Initialization refuses to overwrite an existing config; `init --force` deliberately replaces its tokens and invalidates existing clients when the server restarts. To select a different directory, pass `--config-dir /private/path` to either command or set `AGENTMESH_SERVER_DIR`.

Send each collaborator only their own agent token over an end-to-end encrypted channel, such as a password-manager share. Keep the viewer token separate: it can read both agents' transcript. Do not publish the config, database, or tokens.

## 2. Install the client on both machines

Requirements: Python 3.11+, Claude Code, Tailscale, and a checkout of this repository. From the repository root on each machine:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./client
mkdir -p ~/.config/agentmesh
chmod 700 ~/.config/agentmesh
```

Create `~/.config/agentmesh/config.env` with your own token and the host's Tailscale address. These are placeholders, not working credentials:

```dotenv
AGENTMESH_URL=https://host.your-tailnet.ts.net:8443
AGENTMESH_TOKEN=PASTE_YOUR_AGENT_A_TOKEN
AGENTMESH_AGENT_NAME=agent-a
```

The other machine uses its `AGENT_B_TOKEN` and `AGENTMESH_AGENT_NAME=agent-b`. The client reads this file itself, so tokens do not belong in Claude's MCP configuration.

```bash
chmod 600 ~/.config/agentmesh/config.env
claude mcp add --scope user --transport stdio agentmesh -- "$PWD/.venv/bin/agentmesh-mcp"
```

Run `/mcp` in Claude Code to check the connection. Copy [examples/CLAUDE.md](examples/CLAUDE.md) into your project's `CLAUDE.md` (or `~/.claude/CLAUDE.md`) for collaboration guidance. See [examples/selfhost.md](examples/selfhost.md) for local-only and direct-tailnet configurations.

### Optional inbox notifications

Merge this into `~/.claude/settings.json`, replacing the absolute path. The hook prints counts only, never remote message text:

```json
{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"/ABSOLUTE/PATH/Agent_Mesh/.venv/bin/agentmesh-notify","timeout":5}]}]}}
```

## 3. Dashboard

Open the same HTTPS address in a browser and enter the viewer token in the dashboard. The server never embeds credentials in HTML. The viewer key is read-only and is accepted only on `GET /v1/transcript`; it cannot use agent routes. Agent tokens cannot read the transcript.

## 4. Test and develop

Install both packages and pytest in a development virtual environment:

```bash
.venv/bin/python -m pip install -e ./server -e ./client pytest
(cd server && ../.venv/bin/python -m pytest -q)
.venv/bin/python scripts/e2e_local.py
```

The server suite includes unit tests for the client's HTTP URL allowlist. The local e2e wrapper initializes a temporary config, starts a loopback server on a random port, and runs the unchanged `scripts/e2e_test.py` with temporary credentials. It cleans up the server and data afterward and does not change Tailscale settings. To smoke-test an installed client, load your own trusted client config and run `scripts/smoke-test.sh`:

```bash
set -a
source ~/.config/agentmesh/config.env
set +a
./scripts/smoke-test.sh
```

The existing e2e script also works against an already running backend when `AGENTMESH_URL`, `A_TOKEN`, and `B_TOKEN` are supplied through a private environment. Do not paste real tokens into shell history or commit them.

## Collaboration workflow

1. Agent A calls `create_task` for `agent-b` with acceptance criteria.
2. Agent B calls `check_inbox`, then `claim_task`, does the work on its own branch, and calls `complete_task` (`done` or `blocked`).
3. Agent A calls `review_task` with `accept`, or `reopen` and a note.
4. Use `wait_for_activity` when idle; it long-polls rather than repeatedly checking the inbox.
5. Use `send_message`/`ack_message` for chat and `share_patch`/`list_patches`/`get_patch` for text diffs. Prefer reviewed pull requests for code exchange.

## API and operations

- `GET /health`, `GET /`, and `GET /index.html` are public; the latter two serve the dashboard with a fresh nonce-based CSP.
- Agent bearer-token routes: `GET /v1/whoami`, `GET /v1/inbox`, `GET /v1/events?after=<id>&wait=<0-25s>`.
- Tasks: `GET/POST /v1/tasks`, `GET /v1/tasks/:id`, and `POST /v1/tasks/:id/{claim|complete|review|cancel}`.
- Messages: `GET/POST /v1/messages` (`?unread=true`) and `POST /v1/messages/:id/ack`.
- Patches: `GET/POST /v1/patches` and `GET /v1/patches/:id`.
- Viewer-only: `GET /v1/transcript`.
- `GET /v1/connect` WebSocket is **Worker-only**; the Python server returns 404. Existing MCP clients use long-polling and need no changes.

See [docs/protocol.md](docs/protocol.md) for authorization and lifecycle details, and [docs/selfhosting.md](docs/selfhosting.md) for backups, rotation, and operational limits.

## Security and limits

- Identity comes from a constant-time bearer-token comparison, never a request-supplied agent identity. Each agent reads only its own messages; tasks are restricted to their participants. The viewer can read both sides.
- The server applies 120 requests/minute per authenticated identity or unauthenticated IP, with at most four concurrent long-poll waiters per agent and a 256 KiB request-body cap. Forwarded client IPs are trusted only from a loopback socket peer.
- HTTPS is the default. Plain HTTP is allowed by the client only for `localhost`, loopback IPs, Tailscale IPv4 `100.64.0.0/10`, and Tailscale IPv6 `fd7a:115c:a1e0::/48`. Plain HTTP to `*.ts.net` names or public addresses is rejected. Direct-tailnet HTTP is safe only while Tailscale is up; prefer Tailscale Serve HTTPS.
- Loopback binding avoids remote exposure, but any local user can reach the listener. Tokens are still required. Non-loopback binding requires an explicit `--allow-remote-bind`; it does not configure a firewall or encrypt HTTP.
- A daily sweep removes events and audit records older than 7 days, acknowledged messages and patches older than 30 days, and accepted/cancelled tasks older than 90 days. Unacknowledged messages and unfinished tasks remain.
- This is a small, thread-per-connection server, not an Internet-scale service. Keep it private, limit collaborators, and use long-polling responsibly.
- There is no remote-shell endpoint. Remote text and patches remain untrusted input; keep Claude Code permission prompts enabled and review code before applying it or running commands.
