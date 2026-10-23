# Self-hosted client examples

All values below are placeholders. Put only your own configuration in `~/.config/agentmesh/config.env`, mode 0600. Do not add credentials to the repository or Claude's MCP JSON.

## Two machines through Tailscale Serve (recommended)

On the first client:

```dotenv
AGENTMESH_URL=https://host.your-tailnet.ts.net:8443
AGENTMESH_TOKEN=PASTE_AGENT_A_TOKEN
AGENTMESH_AGENT_NAME=agent-a
```

On the second client:

```dotenv
AGENTMESH_URL=https://host.your-tailnet.ts.net:8443
AGENTMESH_TOKEN=PASTE_AGENT_B_TOKEN
AGENTMESH_AGENT_NAME=agent-b
```

Both machines must be able to reach the host in Tailscale. Use the exact HTTPS address printed by Tailscale Serve. No Cloudflare Access variables are needed.

## Same-machine development

With `agentmesh-server serve` running on the same machine:

```dotenv
AGENTMESH_URL=http://localhost:8787
AGENTMESH_TOKEN=PASTE_AGENT_A_TOKEN
AGENTMESH_AGENT_NAME=agent-a
```

This is loopback HTTP, not a remote URL. Local users can still connect to the port, so keep bearer tokens private.

## Direct Tailscale IP (advanced)

If the administrator explicitly binds the server to its Tailscale address with `--allow-remote-bind`, the client can use a literal address in `100.64.0.0/10` or `fd7a:115c:a1e0::/48`:

```dotenv
AGENTMESH_URL=http://100.101.102.103:8787
AGENTMESH_TOKEN=PASTE_AGENT_A_TOKEN
AGENTMESH_AGENT_NAME=agent-a
```

This HTTP fallback is safe only while Tailscale is up and carries the connection. It is not permission to expose HTTP on a public interface. Plain HTTP to a `*.ts.net` hostname or a public IP is rejected; prefer the HTTPS example above.
