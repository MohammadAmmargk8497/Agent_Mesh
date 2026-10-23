# Self-hosting operations

Follow the [Tailscale quickstart](../README.md) first. The Python server uses only the Python 3.11+ standard library at runtime.

## Configuration and service lifecycle

```bash
agentmesh-server init --config-dir /private/agentmesh
agentmesh-server serve --config-dir /private/agentmesh --host 127.0.0.1 --port 8787
```

`AGENTMESH_SERVER_DIR` selects the same directory when `--config-dir` is omitted; otherwise it defaults to `~/.config/agentmesh-server`. `config` is a JSON file containing `AGENT_A_TOKEN`, `AGENT_B_TOKEN`, and `VIEWER_TOKEN`. `data.db` stores tasks, messages, patches, events, and the audit log. Keep the directory private (0700) and files owner-only (0600).

Run the server as a dedicated unprivileged OS user, under your preferred process supervisor. Stop it with the supervisor or Ctrl-C before moving its data. The process must be running for API and long-poll requests to work; Tailscale Serve does not start AgentMesh for you.

The host's Tailscale service can forward port 8443 to loopback without changing an existing 443 service:

```bash
tailscale serve --bg --https=8443 localhost:8787
```

Remove only that endpoint with `tailscale serve --https=8443 off`. Do not expose the service through public port forwarding. The quickstart's HTTPS endpoint is preferred over direct HTTP.

## Backups

Treat both the config and database as sensitive: the config holds credentials, and the database contains both agents' remote text and audit records. Stop the server, then back up the entire config directory to encrypted storage while preserving owner-only permissions. Restart it after copying. Do not copy a live SQLite database with a plain file copy; use SQLite's backup API if a live backup is required. Restore both files into a private directory and pass that path with `--config-dir`.

The daily retention sweep deletes events and audit records after 7 days, acknowledged messages and patches after 30 days, and accepted/cancelled tasks after 90 days. Unacknowledged messages and unfinished tasks are retained. Retention is cleanup, not a backup policy.

## Rotate tokens without losing data

1. Generate a new high-entropy token with a trusted local password manager or Python's `secrets.token_urlsafe(32)`.
2. In the private JSON config, change only the relevant token value to `new,old` (the two real token values separated by a comma).
3. Restart the server so it reads the new config. Both tokens now authenticate as the same identity.
4. Securely update that client's private `config.env`, then restart its MCP connection.
5. Remove the old token from the JSON config and restart the server again.

Repeat separately for each identity. Keep the agent and viewer tokens distinct. `init --force` is a deliberate all-token replacement, not a graceful rotation: it invalidates clients immediately after the next server start. Never share the viewer token with an agent that should not see both sides of the transcript.

## Direct-tailnet HTTP (advanced)

The server rejects non-loopback binds unless `--allow-remote-bind` is explicit. For example, a user may deliberately bind to their own Tailscale IP and use that literal IP in the client URL. Restrict access using Tailscale policy and the host firewall; this flag does not set either up.

The client permits plain HTTP only for `localhost`, loopback addresses, `100.64.0.0/10`, and `fd7a:115c:a1e0::/48`. It does not resolve names to make an HTTP exception: `http://host.tailnet.ts.net` and `http://100.0.0.1` are rejected. This fallback is safe only while Tailscale is up and the address routes through Tailscale. Prefer `https://host.tailnet.ts.net:8443` via Serve.

## Limits and troubleshooting

- `/health` is public; check it locally before debugging Tailscale. On the host, `curl --fail http://127.0.0.1:8787/health` should return 200.
- A 401 means a missing/invalid bearer token. A viewer key on an agent route, or an agent key on `/v1/transcript`, returns 403.
- A 429 can mean 120 requests within 60 seconds for an identity/IP, or more than four waiting event requests for one agent. Back off rather than retrying in a tight loop.
- When Serve proxies requests, the socket peer is loopback. Only loopback peers may supply `X-Forwarded-For`; the server uses only its last entry, appended by the proxy, never a caller-supplied prefix. An invalid final entry falls back to the socket peer. Non-loopback callers' forwarded headers are not read. No request-supplied agent identity header is trusted.
- Requests larger than 256 KiB return 413; individual API fields have their existing lower limits.
- The server uses one thread per connection and serializes database access. It is intended for a small private pair of agents, not large fan-out or untrusted Internet traffic.
- Any local OS user can connect to a loopback listener. Keep tokens private and avoid hosting on an untrusted multi-user machine.
- `/v1/connect` returns 404 here. Use `/v1/events` long-polling; WebSockets are available only on the optional Worker backend.
