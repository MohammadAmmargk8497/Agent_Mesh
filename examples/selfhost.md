# Self-hosted client examples

For normal setup, follow the README's [Host steps](../README.md#host-steps) and [Friend steps](../README.md#friend-steps). The `agentmesh-setup` assistant asks for your agent name, server address, and hidden token, saves the client settings, and prints the registration command for you to run. It does not connect to the server or run Claude Code commands. See the README for [troubleshooting](../README.md#troubleshooting) and the [glossary](../README.md#glossary).

The examples below show the resulting settings for advanced use; you do not need to create the file by hand. All values are placeholders. Keep your own `~/.config/agentmesh/config.env` private (mode 0600 on POSIX systems, restricted account permissions on Windows). Do not add credentials to this repository or Claude's MCP configuration.

## Two machines through Tailscale Serve (recommended)

On the Host's client, enter `agent-a` in setup, paste the complete HTTPS address printed by Tailscale Serve, and enter the Host's agent token. The resulting file has this form:

```dotenv
AGENTMESH_URL=https://host.your-tailnet.ts.net:8443
AGENTMESH_TOKEN=PASTE_AGENT_A_TOKEN
AGENTMESH_AGENT_NAME=agent-a
```

On the Friend's client, use `agent-b`, the same HTTPS address, and only the Friend's agent token:

```dotenv
AGENTMESH_URL=https://host.your-tailnet.ts.net:8443
AGENTMESH_TOKEN=PASTE_AGENT_B_TOKEN
AGENTMESH_AGENT_NAME=agent-b
```

Both machines must be able to reach the Host in Tailscale. Use the exact HTTPS address printed by Tailscale Serve. No Cloudflare Access variables are needed. The token determines the authenticated identity; changing `AGENTMESH_AGENT_NAME` does not turn one agent's token into the other's.

## Same-machine development

With the server running on the same machine, enter `http://localhost:8787` as the server address in setup:

```dotenv
AGENTMESH_URL=http://localhost:8787
AGENTMESH_TOKEN=PASTE_AGENT_A_TOKEN
AGENTMESH_AGENT_NAME=agent-a
```

This is loopback HTTP, not a remote URL. Local users can still connect to the port, so keep bearer tokens private. A second local client still needs its own agent token and a separate saved configuration; it must not overwrite the first client's settings.

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
