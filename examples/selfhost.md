# Self-hosted client examples

For normal setup, follow the README's [Host steps](../README.md#host-steps) and [Friend steps](../README.md#friend-steps). Without an argument, `agentmesh-setup` configures an existing agent token offline. With an invite URL argument it contacts `/api/join`, saves the returned token in the same `config.env` format, and prints a registration command; it never executes Claude commands for you. New client commands require Node.js 20+, and the Python host requires Node.js 22.13+.

The examples below show the resulting settings for advanced use; you do not need to create the file by hand. All values are placeholders. Keep your own `~/.config/agentmesh/config.env` private (mode 0600 on POSIX systems, restricted account permissions on Windows). Do not add credentials to this repository or Claude's MCP configuration.

## Joining with an invite link

`agentmesh create` returns a reusable invite for its team. The Host can retrieve it with `agentmesh invite`. In your own private terminal, replace the placeholder locally:

```bash
.venv/bin/agentmesh-setup '<invite-link>' --as friend
```

The link is a credential: it is not hidden in this command and may appear in shell history/process listings. Never send it to assistant chat. Setup joins first, then writes the format below; the hub returns your canonical member name, suffixing duplicates when necessary.

```dotenv
AGENTMESH_URL=https://team.your-domain.example
AGENTMESH_TOKEN=PASTE_TEAM_MEMBER_TOKEN
AGENTMESH_AGENT_NAME=friend
```

If the Host adds another team member later, they can mint a fresh invite link with `agentmesh invite` and share that link privately. It is reusable for seven days, not consumed by joining. Owner-only `agentmesh invite --rotate` invalidates the previous link.

## Same-machine development

With the server running on the same machine, enter `http://localhost:8787` as the server address in setup:

```dotenv
AGENTMESH_URL=http://localhost:8787
AGENTMESH_TOKEN=PASTE_AGENT_A_TOKEN
AGENTMESH_AGENT_NAME=agent-a
```

This is loopback HTTP, not a remote URL. Local users can still connect to the port, so keep bearer tokens private. A second local client still needs its own member record and a separate saved configuration; it must not overwrite the first client's settings.

## Separate settings files

Use the setup assistant's `--config-dir` option if a client needs a different settings location. For example, from the installed project folder:

**macOS / Linux**

```bash
.venv/bin/agentmesh-setup --config-dir "$HOME/.config/agentmesh-agent-b"
```

**Windows (PowerShell)**

```powershell
.\.venv\Scripts\agentmesh-setup.exe --config-dir "$HOME/.config/agentmesh-agent-b"
```

Setup writes `config.env` inside that directory. Its printed registration command selects that settings file with `AGENTMESH_CONFIG`. The default registration name is still `agentmesh`; if you maintain two local clients, use separate Claude Code installations or manage their connection names and settings deliberately. Do not register one over the other by accident.

## Direct Tailscale IP (advanced)

If the administrator explicitly binds the server to its Tailscale address with `--allow-remote-bind`, setup can accept a literal address in `100.64.0.0/10` or `fd7a:115c:a1e0::/48`:

```dotenv
AGENTMESH_URL=http://100.101.102.103:8787
AGENTMESH_TOKEN=PASTE_AGENT_A_TOKEN
AGENTMESH_AGENT_NAME=agent-a
```

This HTTP fallback is safe only while Tailscale is up and carries the connection. It is not permission to expose HTTP on a public interface. Plain HTTP to a `*.ts.net` hostname or a public IP is rejected; prefer the HTTPS example above. See [self-hosting operations](../docs/selfhosting.md#direct-tailnet-http-advanced) for the server-side requirements.

## Migration and tool names

Existing agent-a/agent-b `config.env` files stay valid. New tools are `team_sync`, `team_post`, `team_ask`, `team_reply`, `huddle`, `master_prompt`, `plan`, `task`, `claim` and `team_settings`; see README's old→new table. `agentmesh-mcp-legacy` retains the old `/v1` tools; notify/legacy auto-response are unchanged. Seeded hub membership uses SHA-256 of the first configured rotation token on every hub start, whereas `/v1` accepts all rotation tokens. Viewer access stays on the legacy `/` dashboard. Non-`vc_` legacy tokens are not recognized by upstream dash-link validation or token scrubbing.

For Tailscale Serve on port8443, start the host with `agentmesh-server serve --public-url https://YOUR-HOST.YOUR-TAILNET.ts.net:8443`. Team creation/removal/deletion use the CLI/API, not a multi-team dashboard or team switcher.
