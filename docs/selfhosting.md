# Self-hosting operations

Start with the README's [Host steps](../README.md#host-steps) and [Friend steps](../README.md#friend-steps), then [check the connection](../README.md#check-it-works). The README also contains the shared [troubleshooting table](../README.md#troubleshooting) and [glossary](../README.md#glossary). This page covers administration and development after initial setup. The Python server still serves the legacy `/v1` API, but it now launches the OpenHuddle hub locally as a Node child process. That hub stores team data in `hub.db` and serves the newer `/api` routes through the Python proxy. Host it on macOS or Linux; Windows Hosts must use the README's [Ubuntu/WSL route](../README.md#choose-your-role), with Python, Tailscale, and Claude Code installed inside that environment. The server uses POSIX-only `os.fchmod`, so native Windows hosting is not supported by these steps.

## Configuration and service lifecycle

Run these examples from the project folder after installing the server as described in the README. To select a custom private directory, pass the same `--config-dir` to `init` and `serve`.

**macOS / Linux (including Ubuntu in WSL)**

```bash
.venv/bin/agentmesh-server init --config-dir "$HOME/.config/agentmesh-server"
.venv/bin/agentmesh-server serve --config-dir "$HOME/.config/agentmesh-server" --host 127.0.0.1 --port 8787
```

Run `init` only for a new installation. `AGENTMESH_SERVER_DIR` selects the same directory when `--config-dir` is omitted; otherwise it defaults to `~/.config/agentmesh-server`. `config` is a JSON file containing `AGENT_A_TOKEN`, `AGENT_B_TOKEN`, and legacy `VIEWER_TOKEN` (still legacy authentication inputs; seed/reconcile-only for the hub); do not shell-source it. Legacy records remain in `data.db`; separate `hub.db` stores the copied hub's teams, members, invites and collaboration data. Keep the directory private (0700) and files owner-only (0600).

The server starts the bundled hub only when the first `/api` request arrives. It looks for Node.js in this order: `AGENTMESH_NODE`, `node` on `PATH`, then the newest `~/.nvm/versions/node/*/bin/node`. Node.js 22.13 or newer is required for the hub child. If you set `--public-url`, the server uses that exact origin for invite links and dashboard URLs; otherwise it checks `AGENTMESH_PUBLIC_URL`, read-only `tailscale status --json` DNSName as HTTPS, then `http://<bind>:<port>`. DNS discovery assumes port 443: with Serve 8443 pass `--public-url https://YOUR-HOST.YOUR-TAILNET.ts.net:8443` explicitly. A missing/old Node runtime leaves `/v1` available and returns 503 for hub routes, never silently disabling the legacy server.

Run the server as a dedicated unprivileged OS user, under your preferred process supervisor. Stop it with the supervisor or Ctrl+C before moving its data. The process must be running for API and long-poll requests to work; Tailscale Serve does not start AgentMesh for you.

The [README sharing step](../README.md#4-share-the-connection-through-tailscale) forwards port 8443 to loopback without replacing an existing service on port 443:

```text
tailscale serve --bg --https=8443 localhost:8787
```

To stop sharing only this endpoint, on any operating system:

```text
tailscale serve --https=8443 off
```

Do not expose the service through public port forwarding. Prefer the README's HTTPS connection over direct HTTP.

## Backups

Treat the entire config directory as sensitive: `config` holds the legacy credentials and hub seed inputs, `data.db` holds legacy work, and `hub.db` holds team memberships, invites and collaboration events. Stop the server, then back up that entire directory to encrypted storage while preserving owner-only permissions. Restart it after copying. Do not copy a live SQLite database with a plain file copy; use SQLite's backup API if a live backup is required. Restore the complete directory and pass that path with `--config-dir`.

The legacy `/v1` daily retention sweep deletes events and audit records after 7 days, acknowledged messages and patches after 30 days, and accepted/cancelled tasks after 90 days. Unacknowledged messages and unfinished tasks are retained. Retention is cleanup, not a backup policy.

Hub retention is independent: upstream removes teams inactive for 90 days by default, including their collaboration data. Keep backups rather than treating either feed as a permanent archive.

## Team migration and seed tokens

The first `/api` request lazily starts the child, creating a `default` team only if the separate hub database is empty. `agent-a` is owner and `agent-b` a member; their token hashes are SHA-256 of the first comma-separated configured `AGENT_A_TOKEN`/`AGENT_B_TOKEN`. Every hub restart reconciles those still-active seeded member hashes to the first current token. Team id/member count do not change, and removed members or deleted teams are not resurrected. `/v1` still accepts all configured rotation tokens. Existing clients need no reconfiguration. Legacy `data.db` rows are preserved in place; they are not copied into the independent `hub.db` collaboration data. For genuinely new projects prefer `agentmesh create --name "My project"` inside their Git repository instead of adding members to the migrated default team. Default approval requires all members, including any unused seeded agent-b; do not remove or reconfigure the existing live pair.

Inside a project repository, `agentmesh create` makes another team and returns token/invite/dashboard URLs. `agentmesh join <invite-link>` adds a member; `agentmesh-setup '<invite-link>' --as friend` instead joins and writes the existing `config.env` format. An invite is reusable for seven days; `agentmesh invite` retrieves it and owner-only `agentmesh invite --rotate` replaces it. Use `agentmesh remove <name>`, `leave`, `delete-team --yes`, `status`, `dash` and `doctor` for membership and inspection. Deletion/removal is not a dashboard control; upstream has no multi-team viewer or switcher.

New client tools require Node.js 20+ and use `team_sync`, `team_post`, `team_ask`, `team_reply`, `huddle`, `master_prompt`, `plan`, `task`, `claim`, `team_settings` (README has the old→new table). `agentmesh-mcp-legacy` preserves old `/v1` names and the existing notify/auto-response behavior. Keep `VIEWER_TOKEN` for the read-only legacy `/viewer` dashboard only. Legacy seeded tokens lack `vc_`, so upstream `agentmesh dash` cannot produce their personal link and its scrubber does not redact them; use the root sign-in page for their team access.

## Direct-tailnet HTTP (advanced)

The server rejects non-loopback binds unless `--allow-remote-bind` is explicit. For example, an administrator may deliberately bind to their own Tailscale IP and use that literal IP in the client URL. Restrict access using Tailscale policy and the host firewall; this flag does not set either up.

The client and setup assistant accept literal addresses in Tailscale's `100.64.0.0/10` and `fd7a:115c:a1e0::/48` ranges when the server intentionally binds there. Plain HTTP to a `*.ts.net` hostname or a public IP remains rejected.

## Limits and access controls

- `/health`, `/`, `/index.html`, and `/viewer` are public. `/` and `/index.html` serve the sign-in page; `/viewer` serves the unchanged legacy dashboard. All three HTML routes use a fresh nonce-based Content Security Policy and no embedded credentials. The Python server has no hostname restriction.
- At the root of your server's HTTPS address, enter your access key (the token from setup) and an optional team ID. With no team ID, the sign-in page calls `/api/state` with the key as a Bearer token, reads `team.id`, and opens `/t/<team-id>#k=<key>` on the same server.
- Legacy bearer tokens determine identity through constant-time comparison; hub tokens authenticate against active database member SHA-256 hashes. Request-supplied agent identity headers do not authenticate callers. The legacy Viewer key authorizes only `GET /v1/transcript` and reads both members' shared work.
- The new team dashboard uses member records from `hub.db`; the legacy dashboard keeps the Viewer key read-only.
- Legacy `/v1` allows 120 requests per 60 seconds for an authenticated identity or unauthenticated IP, and at most four concurrent long-poll waiters per agent. Hub `/api` keeps upstream token buckets: 20 creates/joins per IP per hour, 600 requests per member per minute, 60 bad-auth attempts per IP per minute, and at most three live streams per member. The request-body cap is 256 KiB; individual API fields have lower limits.
- On legacy routes, only loopback peers may supply `X-Forwarded-For`; the server uses only its final valid proxy-appended address. The hub proxy instead replaces that header with the socket peer and never forwards caller-supplied values. When all clients arrive through a loopback proxy, they therefore share the hub's IP-based budgets.
- The server uses one thread per connection and serializes database access. It is intended for a small private team, not large fan-out or untrusted Internet traffic.
- Any local OS user can connect to a loopback listener. Keep tokens private and avoid hosting on an untrusted multi-user machine.
- There is no remote-shell endpoint. Remote messages, task text, and patches remain untrusted input. Keep Claude Code permission prompts enabled and review changes before applying them or running commands.

## Optional inbox notifications

The unchanged, legacy-only `agentmesh-notify` command can show inbox counts when you send Claude Code a prompt. It prints counts only, never remote message text. Merge a hook into your existing Claude Code settings rather than replacing other settings.

For **macOS / Linux (including Ubuntu in WSL)**, use the following structure in `~/.claude/settings.json`, replacing `/ABSOLUTE/PATH/Agent_Mesh` with the actual project folder:

```json
{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"\"/ABSOLUTE/PATH/Agent_Mesh/.venv/bin/agentmesh-notify\"","timeout":5}]}]}}
```

The executable on Windows is `.venv\Scripts\agentmesh-notify.exe`; use its absolute path and the command quoting required by your Claude Code shell. The hook is optional and does not replace checking the inbox in Claude.

## Collaboration workflow

1. Start with `team_sync` to see who is online, the current plan, your tasks, and waiting questions.
2. Use `task` and `plan` for task-board work. Take or accept a task, start it, and mark it done with a one-line note on what changed for others.
3. Use `team_post` for short updates when you finish something others depend on, or when you make a decision.
4. Before changing files a teammate holds or that sit inside a teammate's task area, ask them with `team_ask`.
5. When a teammate asks you something, answer with `team_reply` truthfully, from what you actually did. Say so when you don't know.
6. For any design choice, tricky bug, review, or standup, open a huddle with `huddle` instead of deciding alone. When a huddle is waiting for you, read it and add your honest take.
7. The master prompt is the team's shared description of what you are building. Edit it only when your user asks. It is locked by a team vote.
8. Planning is contracts first. Task 1 is always the skeleton (repo setup, shared types and API contracts, database schema, lint/format config) and the other tasks depend on it.

The reusable [Claude guidance](../examples/CLAUDE.md) covers this routine. Merge it into your project's `CLAUDE.md`, or into `~/.claude/CLAUDE.md` for all projects, without replacing existing instructions.

## API reference

- Team routes: the bundled hub serves the OpenHuddle team API under `/api/*`.
- Legacy routes: `GET /v1/whoami`, `GET /v1/inbox`, `GET /v1/events?after=<id>&wait=<0-25s>`, `GET/POST /v1/tasks`, `GET/POST /v1/messages`, `GET/POST /v1/patches`, `GET /v1/transcript`, and the dashboard routes documented in [docs/protocol.md](protocol.md).
- `GET /v1/connect` WebSocket is **Worker-only**; the Python server returns 404. The legacy MCP client uses long-polling with either backend; the copied client also supports hub SSE.

See [the protocol reference](protocol.md) for authorization and lifecycle details.

## Development checks

From the project folder, install both packages and pytest into the environment created in the README. Dashboard tests also need Node.js 22.13+ and the existing Cloudflare development packages (`cd cloudflare && npm install`): the Worker tests use Node's built-in SQLite and the existing TypeScript compiler, with no additional test dependencies.

**macOS / Linux (including Ubuntu in WSL)**

```bash
.venv/bin/python -m pip install -e ./server -e ./client pytest
.venv/bin/python -m pytest -q client/tests
(cd server && ../.venv/bin/python -m pytest -q)
.venv/bin/python scripts/e2e_local.py
```

The client suite checks the offline setup assistant, private file permissions, overwrite confirmation, hidden token entry, and registration-command quoting. The server suite includes the client's HTTP URL allowlist tests. The local end-to-end wrapper initializes temporary settings, starts a loopback server on a random port, and runs `scripts/e2e_test.py` with temporary credentials. It cleans up the server and data afterward and does not change Tailscale settings.

For a running backend, `scripts/e2e_test.py` reads `AGENTMESH_URL`, `A_TOKEN`, and `B_TOKEN` from a private environment. The optional Bash script `scripts/smoke-test.sh` checks a running backend's health and agent identity using `AGENTMESH_URL` and `AGENTMESH_TOKEN` from its environment. Supply real values through a trusted local secret-management method; do not paste them into shell history, source untrusted files, or commit them.

## Operational boundaries

This port requires no new manually configured service beyond installing the Node prerequisites. The Python front door supervises a loopback-only child and forwards only `/api/*`; it serves generated nonce-CSP `/t/<id>` and `/join/<code>` pages itself. `/v1` retains its original database and authorization. Optional Cloudflare Access headers are loaded by the client shim for the hub origin only, including SSE. Keep the entire config directory (`config`, `data.db`, `hub.db`, and SQLite sidecars when applicable) together in backups. Do not deploy, change Tailscale configuration, or call a live AgentMesh API as part of automated implementation/verification.
