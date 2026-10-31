# Run at home (staging only)

Run the existing Python server on the owner's Linux PC (or Ubuntu/WSL), with its
bundled Node/OpenHuddle hub and dashboard behind a Cloudflare Tunnel:

```text
agentmesh-home.zahid.win -> Cloudflare Tunnel -> 127.0.0.1:8787
                                              |- /health, /, /v1/* (Python)
                                              `- /api/* (local Node child)
```

The local legacy store is `data.db`; the team store is `hub.db`, both ordinary
SQLite files on the PC. Local message passing uses **no Durable Objects**. The
existing Worker and `agents.zahid.win` stay unchanged and live during staging.
This is not a cutover: do not change existing agent URLs, Worker deployments,
production DNS/routes, or production tunnels. Cloudflare Tunnel still depends
on Cloudflare and the PC's power/network; it does not promise unlimited capacity.
Tailscale is not required for this tunnel-based hosting path.

The commands below are **manual owner setup instructions**, not actions run by
the repository's offline checks. No real import, tunnel provisioning, DNS change
or service activation is performed automatically.

## 1. Install and prepare private settings

Install Python 3.11+, Node.js 22.13+, and
[cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/).
Use the existing server/client packages; the importer has no third-party Python
dependencies. From the project folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./server ./client
```

Keep staging separate from any existing local installation. Use the same private
directory for initialization, import, and serving:

```bash
.venv/bin/agentmesh-server init --config-dir "$HOME/.config/agentmesh-home"
```

Run `init` once, only if this directory has no `config`. **It prints fresh private
legacy tokens**, so use a private terminal and store them securely; do not paste
them into chats or logs. Never use `init --force` to stage a restore. An existing
local `config` should be preserved, not regenerated.

An exported Worker backup contains member token **hashes**, not the raw legacy
seed tokens or the server's private `config`. For restored legacy credentials to
keep working, the owner must separately supply the matching `AGENT_A_TOKEN` and
`AGENT_B_TOKEN` in the private JSON `config`, using a trusted private editor or
secret manager. Do not shell-source it or store it in the repository. Starting
with freshly generated tokens instead changes legacy authentication and
reconciles active seeded hub members to those new hashes on startup. Other
restored member hashes are retained. Preserve credentials deliberately before
testing; no production clients should be pointed at staging.

## 2. Import one completed backup while the server is stopped

Choose a complete run produced by [the existing backup tool](backups.md), not the
parent backup directory or a partial run. Its `manifest.json` must say
`complete: true`; keep the manifest and every table file together. Stop any
server using the destination directory, then run:

```bash
python3 scripts/import_backup_local.py \
  --backup-dir "$HOME/agentmesh-backups/COMPLETED_RUN" \
  --config-dir "$HOME/.config/agentmesh-home"
```

The importer reads only local files, verifies the manifest, schemas, file
digests and row counts, and restores the `room` tables into `data.db` and `hub`
tables into `hub.db`. It does not read the server's token config or call an API.
For a fresh import, check the printed per-table counts against the manifest;
`matched`, `expected`, and `total` must match. The importer also preserves
AUTOINCREMENT high-water marks, maps the Worker's hub `agentmesh_schema` version
to Node's SQLite `user_version` without downgrading it, and recreates the known
hub indexes omitted from table-only exports. Existing table definitions must
match the backup, including constraints; whitespace/comments and unquoted SQL
case are ignored, but alternative equivalent syntax can conservatively fail.
It never migrates a mismatched schema. Import **before** first starting the
server, so startup has not already seeded either database.

By default, either database containing any rows causes refusal. To explicitly
permit an insert-only merge or repeat the same import:

```bash
python3 scripts/import_backup_local.py \
  --backup-dir "$HOME/agentmesh-backups/COMPLETED_RUN" \
  --config-dir "$HOME/.config/agentmesh-home" --replace
```

Despite its name, `--replace` **never replaces or deletes rows**. Existing
primary keys/values win (`INSERT OR IGNORE`); a repeat of the same backup adds
zero rows. Extra destination rows survive a merge, so its final totals can
exceed the manifest even though all backup keys are present. A conflicting key
does not overwrite newer local data. Keep original backup runs immutable and
back up any populated destination before merging. Both stores are checked before
either commits, but the two database-file commits are not cross-file atomic. A
disk/commit failure can leave one file committed: keep the server stopped,
resolve the storage issue, retain the original backup, then rerun with
`--replace`. Import is an offline restore, not ongoing synchronization between
the Worker and the PC.

## 3. Start and inspect the local server

```bash
.venv/bin/agentmesh-server serve \
  --config-dir "$HOME/.config/agentmesh-home" \
  --host 127.0.0.1 --port 8787 \
  --public-url https://agentmesh-home.zahid.win
```

Keep this terminal open. Visit <http://127.0.0.1:8787/health> for `"ok": true`
and <http://127.0.0.1:8787/> for the dashboard. Authenticate there with an
appropriate local/restored member key. Team `/api` traffic lazily starts the
loopback-only bundled Node child and uses local `hub.db`; legacy `/v1` uses local
`data.db`. Do not add `--allow-remote-bind`, open a router port, or expose the
Node child's port. Legacy clients use long-polling locally; `/v1/connect`
WebSocket is Worker-only. Stop with Ctrl+C, and restart with `serve`, not `init`.

## 4. Provision and run a dedicated staging tunnel

The owner must separately provision a **new, locally managed tunnel used only
for staging**, with its credentials JSON under `~/.cloudflared`. Never reuse a
tunnel UUID that already serves production: connectors for a shared UUID can
receive its existing traffic. Do not use a remotely managed tunnel token.

Following Cloudflare's
[locally managed tunnel guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/),
the owner can run these provisioning commands in a private terminal, after
reviewing the account/zone and confirming the staging name is unused:

```bash
cloudflared tunnel login
cloudflared tunnel create agentmesh-home
# Substitute only the NEW staging tunnel UUID returned above:
cloudflared tunnel route dns STAGING_TUNNEL_UUID agentmesh-home.zahid.win
chmod 700 "$HOME/.cloudflared"
chmod 600 "$HOME/.cloudflared/STAGING_TUNNEL_UUID.json"
```

Only the staging hostname may be provisioned. Do not overwrite existing DNS
records or run these commands for `agents.zahid.win`. If Cloudflare Access is
desired, review a separate staging policy; production Access settings are not
copied or changed by this script. Treat the credentials JSON and login
certificate as secrets, and never put either in this repository.

Start the connector in another terminal:

```bash
bash scripts/home_tunnel.sh --tunnel-id STAGING_TUNNEL_UUID
```

The script accepts a UUID, not the tunnel name. The default credentials path is
`~/.cloudflared/<uuid>.json`; `--credentials-file /absolute/path` accepts another
private regular file only **within** `~/.cloudflared`. The directory must be
owner-only (`0700`), credentials `0600` or stricter, and neither may be a symlink.
It never reads or prints credential contents. It atomically writes only the
dedicated private `~/.cloudflared/agentmesh-home.yml`; it leaves the default
`config.yml` and unrelated tunnels alone. It clears inherited `TUNNEL_*`
overrides so an ambient remote token/URL cannot bypass this configuration.

The locally managed configuration has exactly these routes (credentials path
is filled in locally):

```yaml
tunnel: STAGING_TUNNEL_UUID
credentials-file: /home/OWNER/.cloudflared/STAGING_TUNNEL_UUID.json
ingress:
  - hostname: agentmesh-home.zahid.win
    service: http://127.0.0.1:8787
  - service: http_status:404
```

Then it runs `cloudflared tunnel --config <dedicated-file> run <uuid>`; there
are **no** login, create, route, deploy or DNS commands inside the script.
Cloudflare's [ingress configuration reference](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/)
requires the final catch-all rule; here all other hostnames receive 404.
Only after owner provisioning, inspect <https://agentmesh-home.zahid.win/health>
and the staging dashboard. This does not change the production hostname.

## 5. Optional Linux systemd user services

First verify both foreground commands above. To keep both processes alive,
the owner can create these two files under `~/.config/systemd/user/`. Replace
every `/ABSOLUTE/PATH/Agent_Mesh` with the actual checkout and
`STAGING_TUNNEL_UUID` with the dedicated staging UUID. Use absolute paths (quote
them if they contain spaces). Set `AGENTMESH_NODE` to the installed Node 22.13+
executable if the user service's `PATH` does not find it. No unit contains a
credential value.

`agentmesh-home.service`:

```ini
[Unit]
Description=AgentMesh local staging hub

[Service]
Type=simple
UMask=0077
ExecStart=/ABSOLUTE/PATH/Agent_Mesh/.venv/bin/agentmesh-server serve --config-dir %h/.config/agentmesh-home --host 127.0.0.1 --port 8787 --public-url https://agentmesh-home.zahid.win
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`agentmesh-home-tunnel.service`:

```ini
[Unit]
Description=AgentMesh staging Cloudflare Tunnel
Wants=agentmesh-home.service
After=agentmesh-home.service

[Service]
Type=simple
UMask=0077
# Include the actual cloudflared installation directory if it is not /usr/bin.
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/bash /ABSOLUTE/PATH/Agent_Mesh/scripts/home_tunnel.sh --tunnel-id STAGING_TUNNEL_UUID
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Stop foreground instances before starting services (only one server can bind
port 8787), then run manually:

```bash
systemctl --user daemon-reload
systemctl --user enable --now agentmesh-home.service agentmesh-home-tunnel.service
systemctl --user status agentmesh-home.service agentmesh-home-tunnel.service
journalctl --user -u agentmesh-home.service -u agentmesh-home-tunnel.service
```

This uses the user's systemd manager, not `sudo cloudflared service install`
(which installs a system service with different home/config defaults). An
administrator can separately enable user lingering for service startup while
logged out. The PC still needs to remain awake and online. To stop only this
staging setup, stop these two user units; do not stop a production connector.

## 6. Keep backups running

Leave the existing [daily Worker backup timer](backups.md) enabled and pointed at
its current source throughout staging. **Do not** repoint `backup_live.py` to
the local server: the local server does not implement Worker owner-export
routes. Staging writes do not reach the Worker and are not in those exports.

Separately protect the complete local private directory (`config`, `data.db`,
`hub.db`, and SQLite sidecars when present), preferably on encrypted storage.
For a consistent simple snapshot, stop the local server/user service, copy the
entire directory preserving private permissions, and restart it. Do not plain
copy live SQLite files; use SQLite's backup API for live copies. See
[self-hosting backups and retention](selfhosting.md#backups). Keep both Worker
export history and local snapshots; neither the import nor tunnel script
prunes them. A production cutover and any later backup-source change require a
separate reviewed run.
