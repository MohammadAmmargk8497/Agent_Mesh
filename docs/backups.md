# Daily Worker backups to the owner's PC

The Cloudflare deployment supports **paged, owner-only exports** of every
AgentMesh SQL table in legacy `Room` (`default-room`) and team `HubRoom`
(`openhuddle-hub`). The Python backup script saves every run permanently;
neither it nor the daily timer prunes, deletes or overwrites earlier runs.
No additional Python packages are needed (Python 3.11+).

This is a Worker export, not the self-hosted Python server's backup interface.
It backs up data that still exists when the export runs; it cannot recover
already-deleted history or change the existing live-data retention rules.

## Owner configuration

Before using the scripts, an administrator must arrange a reviewed Worker update
with the `ADMIN_EXPORT_LIMITER` binding from `cloudflare/wrangler.toml` and a
private `ADMIN_EXPORT_TOKEN` secret. Use a newly generated, independent token
with at least 32 printable non-space ASCII characters, stored in a password
manager. Never reuse an agent, viewer or member token (including a team owner's
member token). These credentials cannot authorize exports. Existing agent/hub
clients do not need changes.

The implementation and offline checks do **not** deploy the Worker, provision
secrets, install the timer or call the live API. An owner must perform that
operational setup separately. Do not change tunnels or DNS for backups.

On the owner's Linux PC (or WSL with systemd user services), create a private
`~/.config/agentmesh/tokens.env` using a private editor. Make its parent directory
mode `0700` and the regular, non-symlink file mode `0600` (or stricter). Example
contents use placeholders only:

```dotenv
AGENTMESH_URL=https://agents.example.com
ADMIN_EXPORT_TOKEN=REPLACE_WITH_A_NEW_PRIVATE_OWNER_EXPORT_TOKEN
```

If this origin requires Cloudflare Access, also configure both
`CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` in that file. Their names
here differ from the client configuration's `AGENTMESH_CF_ACCESS_*` names.
The file is parsed as data, never sourced as shell commands; simple single- or
double-quoted values are accepted. Matching environment variables override file
values. No token command-line option exists, and errors never print credentials
or server response bodies. Do not put real credentials in this repository.

## Run and schedule

After owner setup, a manual run from the checkout is:

```bash
python3 scripts/backup_live.py
```

By default, each run creates
`~/agentmesh-backups/<unique-UTC-timestamp>/`. Override the destination with
`--backup-dir /absolute/backup/path`. Other controls are `--url HTTPS_ORIGIN`,
`--tokens-file PATH`, `--page-limit 1..1000`, `--timeout SECONDS`, and
`--request-delay SECONDS` (default `0.55`, under the dedicated 120/minute budget).
HTTPS is required and redirects are rejected so credentials cannot leak to
another origin. `--allow-local-http` permits loopback HTTP only for offline tests.

To install the daily systemd **user** service/timer, run this on the owner's PC:

```bash
bash scripts/install_backup_timer.sh --project /absolute/path/to/Agent_Mesh
```

It writes `~/.config/systemd/user/agentmesh-backup.service` and
`agentmesh-backup.timer`, reloads the user manager, and enables the timer.
`--backup-dir /absolute/backup/path` is also supported. The unit contains no
credentials: Python reads `~/.config/agentmesh/tokens.env` at each execution.
`OnCalendar=daily` runs once daily in the PC's local timezone; `Persistent=true`
catches a missed run when the user timer starts again. The service has no
startup time limit, so large exports can finish. No schedule is installed until
the installer is explicitly invoked.

The PC must be running with a user systemd manager and network access. An owner
can separately enable user lingering if backups must run while logged out.
Inspect scheduling and failures with:

```bash
systemctl --user list-timers agentmesh-backup.timer
systemctl --user status agentmesh-backup.service
journalctl --user -u agentmesh-backup.service
```

## Backup layout and verification

Each table has one JSON file, named `<store>-<SHA256-of-table-name>.json` to avoid
path traversal and collisions between the two stores. Each file is a JSON array
of `{ "rowid": "123", "data": { ...original columns... } }`. Rowids are signed
SQLite int64 decimal **strings**, including negative/zero values and values
beyond JavaScript's safe-integer range. BLOB cells use `{ "$base64": "..." }`;
current AgentMesh tables use text/numeric/null cells. Unsupported or unsafe
numeric values fail rather than silently losing data.

Only a successful run has a `manifest.json` with `complete: true`. It records
the format version, UTC start, source origin, each table's store/name/CREATE SQL,
expected and pulled counts, filename, and SHA-256 of the exact file bytes. A
retained `.manifest.pending` is the same completed manifest used for exclusive
publication; its presence alone does not indicate success. Files are flushed
before manifest publication. Run directories/files are created privately and
exclusively, never reused or replaced.

The script validates every page identity, ordering and cursor, checks pulled
counts against the initial inventory, and rechecks the complete table inventory
after downloading. It exits nonzero on any page/HTTP/JSON/I/O/count error; it
does not skip pages or silently accept a partial export. Failed run directories
are retained without a completed manifest. Earlier backups remain untouched.

**Consistency limit:** separate pages and the two Durable Objects are not one
transactional point-in-time snapshot. Count/schema changes are detected, but
same-count edits or delete/insert pairs can escape that check. Export during a
quiet period for the strongest consistency; retry changed inventories later.
Disk space must be monitored because every run is kept forever. Protect backup
storage: it contains private conversations, invitation secrets and token hashes.
Restoration tooling is not included; preserve the matching source/schema too.

## Owner export protocol

Both routes accept only `GET` with `Authorization: Bearer <owner-export-token>`.
The Worker SHA-256 hash-compares the owner secret, refuses configured agent and
retired viewer credentials, and refuses any hash stored for a hub member (even
if the owner secret was accidentally set equal to one). Member checks bypass
hub authentication, seeding and presence updates. Exports use only schema/SELECT
queries, never normal mutable application routes.
Schema initialization/migration and alarm scheduling are deferred until normal
application traffic, not exports. An untouched store therefore exports no tables;
the PC tool fails closed on an inventory without both initialized stores.

- `GET /admin/export/tables` returns
  `{ "tables": [{ "store": "room|hub", "name": "messages", "count": 42, "sql": "CREATE TABLE ..." }] }`.
  All application tables, the hub schema-version table, and `sqlite_sequence`
  high-water marks are included. Cloudflare `_cf_*` and other SQLite internal
  tables are not AgentMesh data and are excluded.
- `GET /admin/export/page?store=room|hub&table=<name>&after=<rowid>&limit=<n>`
  returns `{ "store": "room", "table": "messages", "rows": [...], "next_after": "123", "done": false }`.
  Omit `after` initially to include negative and zero rowids. Subsequent pages
  use `rowid > after ORDER BY rowid`; names are checked against the actual schema
  and safely quoted. `limit` defaults to 100 and is capped at 1000.
  Pages also have a 2 MiB byte budget; a short nonempty page is **not** the end.
  Continue until an empty page with `done: true` and `next_after: null`.
  A single row over the budget fails explicitly instead of being truncated.

Responses are `no-store`. Unauthorized credentials receive `401`, writes `405`,
invalid parameters `400`, unknown tables `404`, excessive requests `429`, and
missing/unavailable dedicated rate limiting or missing/short owner configuration
`503`. Export errors never fall back to the normal agent limiter. Cloudflare
Workers compatibility date is `2026-10-01`; the binding and RPC implementation
use the existing Workers types/toolchain without new dependencies.

## Offline verification

From the repository root (no live AgentMesh requests or deployment):

```bash
cd cloudflare && npm run typecheck && npm test >/dev/null && echo worker-ok
cd ../server && PYTHONPATH=src python3 -m pytest -q tests -k 'backup or export'
cd .. && bash -n scripts/install_backup_timer.sh && python3 -m py_compile scripts/backup_live.py && echo scripts-ok
```

Tests cover constructor-inclusive read-only cold starts, exact-once paging,
non-owner rejection, missing pages/count checks, digests, permanent run retention,
redirect rejection and timer installation using a fake systemctl.
