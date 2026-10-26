# Third-party notices

Upstream: https://github.com/nika110/openhuddle (MIT, commit `59105e5`).

AgentMesh vendors OpenHuddle 59105e5 in three places:

- `server/src/agentmesh_server/vendor/openhuddle/` — the Python-hosted vendored tree.
- `client/src/agentmesh_mcp/vendor/openhuddle/` — the client/MCP vendored tree.
- `cloudflare/src/openhuddle/` — the Cloudflare Worker port.

The generated dashboard pages in `server/src/agentmesh_server/` and `cloudflare/src/` are built from OpenHuddle dashboard assets and carry the same notice trail.

## `server/src/agentmesh_server/vendor/openhuddle/`

- `server/src/agentmesh_server/vendor/openhuddle/LICENSE` — verbatim OpenHuddle MIT license.
- `server/src/agentmesh_server/vendor/openhuddle/UPSTREAM.sha256` — generated checksum manifest for the vendored tree.
- `server/src/agentmesh_server/vendor/openhuddle/agentmesh-boot.js` — original AgentMesh bootstrapper that starts the bundled hub under the Python server.
- `server/src/agentmesh_server/vendor/openhuddle/agentmesh-seed.js` — original AgentMesh seed adapter shared with the other ports.
- `server/src/agentmesh_server/vendor/openhuddle/bin/openhuddle.js` — verbatim CLI entry point.
- `server/src/agentmesh_server/vendor/openhuddle/commands/create.md` — verbatim command help.
- `server/src/agentmesh_server/vendor/openhuddle/commands/huddle.md` — verbatim command help.
- `server/src/agentmesh_server/vendor/openhuddle/commands/join.md` — verbatim command help.
- `server/src/agentmesh_server/vendor/openhuddle/commands/status.md` — verbatim command help.
- `server/src/agentmesh_server/vendor/openhuddle/package.json` — vendored package manifest.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/api.js` — verbatim client API helper.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/cli.js` — verbatim client CLI.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/config.js` — verbatim client config helper.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/format.js` — verbatim client formatting helpers.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/git.js` — verbatim Git helpers.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/hooks.js` — verbatim hook helpers.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/mcp.js` — verbatim MCP bridge.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/news.js` — verbatim news helper.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/tools.js` — verbatim team-tool definitions.
- `server/src/agentmesh_server/vendor/openhuddle/src/client/wire.js` — verbatim wire-format helpers.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/asks.js` — verbatim hub feature module.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/claims.js` — verbatim hub feature module.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/db.js` — verbatim hub database helper.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/errors.js` — verbatim hub error helpers.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/hub.js` — verbatim hub core.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/huddles.js` — verbatim hub feature module.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/base.css` — adapted: AgentMesh palette and font variables.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/dashboard.css` — verbatim dashboard asset.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/dashboard.html` — verbatim dashboard asset.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/dashboard.js` — adapted: AgentMesh session storage namespace.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/favicon.svg` — verbatim dashboard asset.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/join.html` — verbatim dashboard asset.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/landing.html` — verbatim dashboard asset.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/mascot.svg` — verbatim dashboard asset.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/site.css` — verbatim dashboard asset.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/pages/site.js` — verbatim dashboard asset.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/prompt.js` — verbatim hub feature module.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/server.js` — verbatim hub server.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/tasks.js` — verbatim hub feature module.
- `server/src/agentmesh_server/vendor/openhuddle/src/hub/text.js` — verbatim hub feature module.
- `server/src/agentmesh_server/vendor/openhuddle/src/shared/glob.js` — verbatim shared helper.
- `server/src/agentmesh_server/vendor/openhuddle/src/shared/ids.js` — verbatim shared helper.
- `server/src/agentmesh_server/vendor/openhuddle/src/shared/paths.js` — verbatim shared helper.
- `server/src/agentmesh_server/vendor/openhuddle/src/shared/plain.js` — verbatim shared helper.
- `server/src/agentmesh_server/vendor/openhuddle/src/shared/protocol.js` — verbatim shared protocol definitions.
- `server/src/agentmesh_server/vendor/openhuddle/src/shared/scrub.js` — verbatim shared helper.

## `client/src/agentmesh_mcp/vendor/openhuddle/`

- `client/src/agentmesh_mcp/vendor/openhuddle/LICENSE` — verbatim OpenHuddle MIT license.
- `client/src/agentmesh_mcp/vendor/openhuddle/UPSTREAM.sha256` — generated checksum manifest for the vendored tree.
- `client/src/agentmesh_mcp/vendor/openhuddle/agentmesh-client-boot.js` — original AgentMesh bootstrapper that loads the vendored client under the AgentMesh shim.
- `client/src/agentmesh_mcp/vendor/openhuddle/bin/openhuddle.js` — adapted only to import the AgentMesh config/Access boundary and pass its CLI mode; upstream exit handling and `err.message` reporting are unchanged.
- `client/src/agentmesh_mcp/vendor/openhuddle/commands/create.md` — verbatim command help.
- `client/src/agentmesh_mcp/vendor/openhuddle/commands/huddle.md` — verbatim command help.
- `client/src/agentmesh_mcp/vendor/openhuddle/commands/join.md` — verbatim command help.
- `client/src/agentmesh_mcp/vendor/openhuddle/commands/status.md` — verbatim command help.
- `client/src/agentmesh_mcp/vendor/openhuddle/package.json` — vendored package manifest.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/agentmesh-boundary.js` — original AgentMesh boundary shim for local config loading and hub-origin Access headers.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/api.js` — verbatim client API helper.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/cli.js` — verbatim client CLI.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/config.js` — verbatim client config helper.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/format.js` — verbatim client formatting helpers.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/git.js` — verbatim Git helpers.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/hooks.js` — verbatim hook helpers.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/mcp.js` — verbatim MCP bridge.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/news.js` — verbatim news helper.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/tools.js` — verbatim team-tool definitions.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/wire.js` — verbatim wire-format helpers.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/shared/glob.js` — verbatim shared helper.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/shared/ids.js` — verbatim shared helper.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/shared/paths.js` — verbatim shared helper.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/shared/plain.js` — verbatim shared helper.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/shared/protocol.js` — verbatim shared protocol definitions.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/shared/scrub.js` — verbatim shared helper.

## `cloudflare/src/openhuddle/`

- `cloudflare/src/openhuddle/LICENSE` — verbatim OpenHuddle MIT license.
- `cloudflare/src/openhuddle/UPSTREAM.sha256` — generated checksum manifest for the port.
- `cloudflare/src/openhuddle/agentmesh-seed.js` — original AgentMesh seed adapter shared with the other ports.
- `cloudflare/src/openhuddle/do-sqlite.ts` — original storage adapter for Durable Object SQLite.
- `cloudflare/src/openhuddle/hub-room.ts` — adapted: Worker Request/Response routing, SSE and Durable Object lifecycle.
- `cloudflare/src/openhuddle/package.json` — vendored package manifest.
- `cloudflare/src/openhuddle/src/hub/asks.js` — verbatim hub feature module.
- `cloudflare/src/openhuddle/src/hub/claims.js` — verbatim hub feature module.
- `cloudflare/src/openhuddle/src/hub/db-do.js` — adapted: DO SQL adapter, schema-version table and transactionSync (PRAGMA user_version unsupported).
- `cloudflare/src/openhuddle/src/hub/db.js` — verbatim hub database helper.
- `cloudflare/src/openhuddle/src/hub/errors.js` — verbatim hub error helpers.
- `cloudflare/src/openhuddle/src/hub/hub.js` — adapted: Durable Object transactionSync and no filesystem backups.
- `cloudflare/src/openhuddle/src/hub/huddles.js` — verbatim hub feature module.
- `cloudflare/src/openhuddle/src/hub/prompt.js` — verbatim hub feature module.
- `cloudflare/src/openhuddle/src/hub/server.js` — verbatim hub server.
- `cloudflare/src/openhuddle/src/hub/tasks.js` — verbatim hub feature module.
- `cloudflare/src/openhuddle/src/hub/text.js` — verbatim hub feature module.
- `cloudflare/src/openhuddle/src/shared/glob.js` — verbatim shared helper.
- `cloudflare/src/openhuddle/src/shared/ids.js` — verbatim shared helper.
- `cloudflare/src/openhuddle/src/shared/paths.js` — verbatim shared helper.
- `cloudflare/src/openhuddle/src/shared/plain.js` — verbatim shared helper.
- `cloudflare/src/openhuddle/src/shared/protocol.js` — adapted: import package JSON instead of node:fs.
- `cloudflare/src/openhuddle/src/shared/scrub.js` — verbatim shared helper.

## Generated dashboard pages

- `server/src/agentmesh_server/hub_dashboard.html` — generated from OpenHuddle dashboard assets, with AgentMesh theme substitutions and nonce support.
- `server/src/agentmesh_server/hub_join.html` — generated from the OpenHuddle join page and shared shell assets.
- `cloudflare/src/hub_dashboard.html` — generated from OpenHuddle dashboard assets, with AgentMesh theme substitutions and nonce support.
- `cloudflare/src/hub_join.html` — generated from the OpenHuddle join page and shared shell assets.

## Boundary exceptions and original adapters

The vendored hub layout and domain logic are preserved in their source assets. The unified `dashboard.html` copies the generated hub page's member, board, huddle, prompt, invite and feed behavior into the existing AgentMesh viewer layout and theme. Original additions are question/claim controls over existing hub routes, member presence chips, session-safe viewer integration and default-team-only read adapters. Other named adaptations remain Worker DO SQL/version/transaction/lifecycle routing and backup boundaries; client config.env/Cloudflare Access bootstrap; original token seed/reconciliation; and dashboard palette/font variables, sessionStorage namespace and nonce-CSP inlining. No upstream team-token, consume-once invite, dashboard membership-management or multi-team feature is invented.

Original AgentMesh integration (not copied from upstream):
- `client/src/agentmesh_mcp/vendor/openhuddle/agentmesh-client-boot.js` — Python shim's Node entry point.
- `client/src/agentmesh_mcp/vendor/openhuddle/src/client/agentmesh-boundary.js` — config.env loader, origin-scoped Access fetch wrapper and upstream saveConfig/findProject bootstrap, also reached by the adapted installed bin.
- `cloudflare/src/openhuddle/do-sqlite.ts` — Durable Object SQL compatibility adapter.

The generated pages listed above derive from upstream `src/hub/pages/dashboard.html`, `dashboard.js`, `dashboard.css`, `base.css`, `join.html`, `site.css`, `site.js`, `favicon.svg` and `mascot.svg`. The unified AgentMesh `server/src/agentmesh_server/dashboard.html` and `cloudflare/src/dashboard.html` remain adapted derivatives of those dashboard sources, with their original `/v1` behavior preserved alongside copied `/api` behavior. See [unified-dashboard.md](unified-dashboard.md) for the integration boundaries.

## OpenHuddle MIT notice

```text
MIT License

Copyright (c) 2026 nika110

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
