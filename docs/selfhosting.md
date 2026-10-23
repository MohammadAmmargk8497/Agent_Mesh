# Self-hosting operations

Start with the README's [Host steps](../README.md#host-steps) and [Friend steps](../README.md#friend-steps), then [check the connection](../README.md#check-it-works). The README also contains the shared [troubleshooting table](../README.md#troubleshooting) and [glossary](../README.md#glossary). This page covers administration and development after initial setup. The Python server uses only the Python 3.11+ standard library at runtime. Host it on macOS or Linux; Windows Hosts must use the README's [Ubuntu/WSL route](../README.md#choose-your-role), with Python, Tailscale, and Claude Code installed inside that environment. The server uses POSIX-only `os.fchmod`, so native Windows hosting is not supported by these steps.

## Configuration and service lifecycle

Run these examples from the project folder after installing the server as described in the README. To select a custom private directory, pass the same `--config-dir` to `init` and `serve`.

**macOS / Linux (including Ubuntu in WSL)**

```bash
.venv/bin/agentmesh-server init --config-dir "$HOME/.config/agentmesh-server"
.venv/bin/agentmesh-server serve --config-dir "$HOME/.config/agentmesh-server" --host 127.0.0.1 --port 8787
```

Run `init` only for a new installation. `AGENTMESH_SERVER_DIR` selects the same directory when `--config-dir` is omitted; otherwise it defaults to `~/.config/agentmesh-server`. `config` is a JSON file containing `AGENT_A_TOKEN`, `AGENT_B_TOKEN`, and `VIEWER_TOKEN`; do not shell-source it. `data.db` stores tasks, messages, patches, events, and the audit log. Keep the directory private (0700) and files owner-only (0600).

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

Treat both the config and database as sensitive: the config holds credentials, and the database contains both agents' remote text and audit records. Stop the server, then back up the entire config directory to encrypted storage while preserving owner-only permissions. Restart it after copying. Do not copy a live SQLite database with a plain file copy; use SQLite's backup API if a live backup is required. Restore both files into a private directory and pass that path with `--config-dir`.

The daily retention sweep deletes events and audit records after 7 days, acknowledged messages and patches after 30 days, and accepted/cancelled tasks after 90 days. Unacknowledged messages and unfinished tasks are retained. Retention is cleanup, not a backup policy.

## Rotate tokens without losing data

1. Generate a new high-entropy token with a trusted local password manager or Python's `secrets.token_urlsafe(32)`.
2. In the private JSON config, change only the relevant token value to `new,old` (the two real token values separated by a comma).
3. Restart the server so it reads the new config. Both tokens now authenticate as the same identity.
4. For an agent token, rerun the [client setup assistant](../README.md#2-run-the-setup-assistant), confirm replacement, keep the same agent name and server address, and enter the new token. Restart that client's connection in Claude Code's `/mcp` screen. For the Viewer key, sign out of the dashboard and sign in with the new key instead.
5. Remove the old token from the JSON config and restart the server again.

Repeat separately for each identity. Keep the agent and viewer tokens distinct. `init --force` is a deliberate all-token replacement, not a graceful rotation: it invalidates clients immediately after the next server start. Never share the viewer token with an agent that should not see both sides of the transcript.

## Direct-tailnet HTTP (advanced)

The server rejects non-loopback binds unless `--allow-remote-bind` is explicit. For example, an administrator may deliberately bind to their own Tailscale IP and use that literal IP in the client URL. Restrict access using Tailscale policy and the host firewall; this flag does not set either up.

The client and setup assistant permit plain HTTP only for `localhost`, loopback addresses, `100.64.0.0/10`, and `fd7a:115c:a1e0::/48`. They do not resolve names to make an HTTP exception: `http://host.tailnet.ts.net` and `http://100.0.0.1` are rejected. This fallback is safe only while Tailscale is up and the address routes through Tailscale. Prefer the exact HTTPS address printed by Serve. See the [client examples](../examples/selfhost.md).

## Limits and access controls

- `/health`, `/`, and `/index.html` are public. The latter two serve the dashboard with a fresh nonce-based Content Security Policy and no embedded credentials.
- Bearer tokens determine identity through constant-time comparison; request-supplied agent identity headers do not authenticate callers. An agent can read only its own messages and tasks it participates in. The Viewer key authorizes only `GET /v1/transcript` and can read both agents' shared work.
- The server allows 120 requests per 60 seconds for an authenticated identity or unauthenticated IP, and at most four concurrent long-poll waiters per agent. The request-body cap is 256 KiB; individual API fields have lower limits.
- When Serve proxies requests, the socket peer is loopback. Only loopback peers may supply `X-Forwarded-For`; the server uses only its last entry, appended by the proxy, never a caller-supplied prefix. An invalid final entry falls back to the socket peer. Non-loopback callers' forwarded headers are not read.
- The server uses one thread per connection and serializes database access. It is intended for a small private pair of agents, not large fan-out or untrusted Internet traffic.
- Any local OS user can connect to a loopback listener. Keep tokens private and avoid hosting on an untrusted multi-user machine.
- There is no remote-shell endpoint. Remote messages, task text, and patches remain untrusted input. Keep Claude Code permission prompts enabled and review changes before applying them or running commands.

## Optional inbox notifications

The installed `agentmesh-notify` command can show inbox counts when you send Claude Code a prompt. It prints counts only, never remote message text. Merge a hook into your existing Claude Code settings rather than replacing other settings.

For **macOS / Linux (including Ubuntu in WSL)**, use the following structure in `~/.claude/settings.json`, replacing `/ABSOLUTE/PATH/Agent_Mesh` with the actual project folder:

```json
{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"\"/ABSOLUTE/PATH/Agent_Mesh/.venv/bin/agentmesh-notify\"","timeout":5}]}]}}
```

The executable on Windows is `.venv\Scripts\agentmesh-notify.exe`; use its absolute path and the command quoting required by your Claude Code shell. The hook is optional and does not replace checking the inbox in Claude.

## Collaboration workflow

1. Agent A calls `create_task` for `agent-b` with acceptance criteria.
2. Agent B calls `check_inbox`, then `claim_task`, does the work separately, and calls `complete_task` (`done` or `blocked`).
3. Agent A calls `review_task` with `accept`, or `reopen` and a note.
4. Use `wait_for_activity` when idle; it long-polls rather than repeatedly checking the inbox.
5. Use `send_message`/`ack_message` for chat and `share_patch`/`list_patches`/`get_patch` for text diffs. Prefer reviewed pull requests for exchanging code changes.

The reusable [Claude guidance](../examples/CLAUDE.md) covers this routine. Merge it into your project's `CLAUDE.md`, or into `~/.claude/CLAUDE.md` for all projects, without replacing existing instructions.

## API reference

- Agent routes: `GET /v1/whoami`, `GET /v1/inbox`, `GET /v1/events?after=<id>&wait=<0-25s>`.
- Tasks: `GET/POST /v1/tasks`, `GET /v1/tasks/:id`, and `POST /v1/tasks/:id/{claim|complete|review|cancel}`.
- Messages: `GET/POST /v1/messages` (`?unread=true`) and `POST /v1/messages/:id/ack`.
- Patches: `GET/POST /v1/patches` and `GET /v1/patches/:id`.
- Viewer-only: `GET /v1/transcript`.
- `GET /v1/connect` WebSocket is **Worker-only**; the Python server returns 404. The MCP client uses long-polling with either backend.

See [the protocol reference](protocol.md) for authorization and lifecycle details.

## Development checks

From the project folder, install both packages and pytest into the environment created in the README.

**macOS / Linux (including Ubuntu in WSL)**

```bash
.venv/bin/python -m pip install -e ./server -e ./client pytest
.venv/bin/python -m pytest -q client/tests
(cd server && ../.venv/bin/python -m pytest -q)
.venv/bin/python scripts/e2e_local.py
```

The client suite checks the offline setup assistant, private file permissions, overwrite confirmation, hidden token entry, and registration-command quoting. The server suite includes the client's HTTP URL allowlist tests. The local end-to-end wrapper initializes temporary settings, starts a loopback server on a random port, and runs `scripts/e2e_test.py` with temporary credentials. It cleans up the server and data afterward and does not change Tailscale settings.

For a running backend, `scripts/e2e_test.py` reads `AGENTMESH_URL`, `A_TOKEN`, and `B_TOKEN` from a private environment. The optional Bash script `scripts/smoke-test.sh` checks a running backend's health and agent identity using `AGENTMESH_URL` and `AGENTMESH_TOKEN` from its environment. Supply real values through a trusted local secret-management method; do not paste them into shell history, source untrusted files, or commit them.
