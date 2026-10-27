# Owner rollback and insert-only backup merge

`scripts/rollback_merge.py` restores the two Cloudflare SQLite Durable Objects
separately, then re-adds backup rows whose primary keys are absent. It does not
deploy the Worker, provision credentials, change tunnels/DNS, or modify/delete
any local backup files. Implementation tests use local SQLite only; **no live
restore has been performed by this change**.

This command changes live data when an owner runs it without `--dry-run`.
The existing `ADMIN_EXPORT_TOKEN` now authorizes restoration and imports as well
as exports; protect it as a recovery credential. Agent, retired viewer, ordinary
member, and team-owner member tokens cannot authorize these operations. All
routes share the existing fail-closed `ADMIN_EXPORT_LIMITER` budget.

## Before an operational restore

- Arrange a separately reviewed deployment of the recovery routes. This task
  does not deploy or contact the live API.
- Keep the complete, just-taken [owner backup](backups.md), including its matching
  source/schema. The default backup root is `~/agentmesh-backups`.
- Stop normal application writers for the operation. The two objects and paged
  exports are not one transaction. New writes after the backup are not protected
  by it. Restoring discards changes after the restore point until the merge
  re-adds their missing backup keys; it is not a general historical union.
- Resolve schema compatibility first: import never creates tables, migrates,
  disables foreign keys, or bypasses constraints. A point before a table/column
  was introduced cannot be repaired with an insert-only endpoint alone.
- Reuse the private credential configuration documented in `backups.md`. No
  token command-line argument is accepted; redirects are rejected and errors
  omit response bodies and credentials.

An owner can first inspect a specific completed run without restoring/importing:

```bash
python3 scripts/rollback_merge.py --backup-dir ~/agentmesh-backups/COMPLETED_RUN --dry-run
```

After the separate operational review, the same command **without `--dry-run`**
performs restoration and merging. There is no timer for this operation.
Supplying the backup root selects its newest complete manifest by creation time;
incomplete runs are ignored, never removed. Prefer an explicit run directory to
pin the recovery source. A malformed completed backup fails closed.

Options also include `--url HTTPS_ORIGIN`, `--tokens-file PATH`,
`--page-limit 1..1000`, `--request-delay SECONDS` (default `0.55`),
`--timeout SECONDS` (per request, default `60`), and `--wait-timeout SECONDS`
(restart polling, default `120`). `--allow-local-http` permits only loopback
fixtures, not insecure production HTTP. The configured origin must match the
backup manifest's source; there is no cross-origin override.

## Processing and guarantees

1. Read all backup tables without writing to the backup. Require `complete: true`,
   format version 1, both stores, unique safe filenames, valid schemas, exact
   counts, matching SHA-256 digests, ordered rowids, valid cells and unique keys.
   Check import pageability, dependency ordering and the current live schema
   before scheduling a restore. No backup SQL is executed on disk or remotely;
   schema inspection uses a single CREATE statement in an in-memory database.
2. Send dry-run restore requests for **both** stores with `time=0`, the sentinel
   for the oldest supported lookup. Print the resolved epoch milliseconds,
   human-readable time and bookmark for each store.
3. Restore `room`, then `hub`, waiting for exports after each restart. The real
   response prints the actual lookup time too: the retention boundary can move
   between preview and execution. Restore POSTs are never automatically retried
   after a network error; inspect live state separately if an outcome is unknown.
4. Export each live table's keys and import only missing backup rows, parents
   before foreign-key children. Requests contain at most 1000 rows and 2 MiB of
   UTF-8 JSON. Original column values, including BLOBs, are retained. An existing
   key wins even when its values differ from the backup. No updates or deletes
   are issued; a second merge against the merged state adds nothing.
5. Re-export every backup table. Print `PASS`/`FAIL` per table based on **every
   backup key** existing, counts being at least the backup counts, and exported
   counts matching the inventory. Print the insert/skip summary; exit 1 on any
   failure. A same-count replacement cannot conceal a missing backup key.

Declared primary keys (including composites) determine row identity, not the
export rowid. Keyless tables and NULL primary keys use their exact signed int64
rowid. `sqlite_sequence` is special only because SQLite declares no key for it:
the table name is its logical key. Existing sequence rows are not changed,
including lower high-water marks; normal AUTOINCREMENT behavior still applies
to inserted application rows. This tool verifies key coverage, not equality of
values on existing rows or restoration of all sequence high-water values.

Failures never trigger cleanup or a second rollback. Imports are atomic per
batch, not across tables/stores; earlier successful batches remain if a later
batch fails. Keep the printed bookmarks and resolve failures before repeating
an operational rollback. There is no automatic recovery from an incompatible
historical schema, foreign-key cycle, unique-key collision on a different key,
or import-trigger restriction. Missing-key verification fails rather than
overwriting a conflicting row.

## Worker recovery protocol

Both routes require `POST` and `Authorization: Bearer <owner-export-token>`.
The authorization and limiter are the same as the export routes, including
rejecting known member hashes without normal hub authentication/seeding.

- `POST /admin/restore?store=room|hub&time=<epoch-ms>&dry_run=1|0`
  computes `ctx.storage.getBookmarkForTime(time)` inside the selected object.
  Cloudflare documents a 30-day lookup window; older requests are clamped to
  that boundary with a one-second in-flight timing allowance. `time` in the
  response is the effective **lookup time**, not an undocumented bookmark commit
  timestamp. An object created later can resolve to its earliest available state;
  unsupported/unavailable bookmarks fail rather than fabricating a restore.
  A dry run returns `{store, requested_time, time, bookmark, dry_run: true}`
  without scheduling a restore, migrations, seeding, alarms, or data writes.
  A real request awaits `onNextSessionRestoreBookmark(bookmark)` and returns its
  undo bookmark metadata to the outer Worker before a separate RPC invokes
  `ctx.abort()`. Abort intentionally disconnects that RPC and cannot return an
  object response. Pending instances refuse normal access/exports/imports until
  restart, so polling cannot mistake the pre-restore instance for restored data.
  A real response also includes `undo_bookmark` and `restore_scheduled: true`;
  readiness polling, not that response alone, confirms the restart.
- `POST /admin/import?store=room|hub&table=<name>` accepts
  `{"rows":[{"rowid":"123","data":{"id":"example","body":"original"}}]}`.
  Complete plain column objects are also accepted for declared non-NULL primary
  keys. Export envelopes are necessary for keyless/NULL-key rows. At most 1000
  rows and 2 MiB per request are accepted. Table names are checked against the
  exportable schema; column names are checked with `PRAGMA table_info` and
  quoted. All values are bound. Unknown/missing columns, invalid cells, unsafe
  numbers and noncanonical BLOB base64 are rejected. In one synchronous storage
  transaction, only `INSERT OR IGNORE` is used, returning `{store, table,
  inserted, skipped}`. FK violations roll back that batch. Triggered imports
  fail closed when an insert could have side effects on existing data.

Responses are `no-store`. Missing or rejected credentials receive `401`, wrong
methods `405`, invalid input `400`, unknown tables `404`, incompatible imports
`409`, excessive requests `429`, and unavailable owner configuration/limiting
`503`. Bookmark lookup/scheduling failures are not suppressed.

Cloudflare API behavior was checked against its official
[SQLite storage API](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
and [state/abort API](https://developers.cloudflare.com/durable-objects/api/state/)
on `2026-10-26`; the Worker compatibility date remains `2026-10-01`.

## Offline verification

From the repository root, separately:

```bash
cd cloudflare && npm run typecheck && npm test >/dev/null && echo worker-ok
cd server && PYTHONPATH=src python3 -m pytest -q tests -k 'rollback or merge or backup'
python3 -m py_compile scripts/rollback_merge.py && echo script-ok
```

Worker tests exercise owner authentication, dry-run nonmutation, restart
sequencing and insert-only SQLite behavior. Python tests use sample SQLite
databases and local fixtures, including existing-value preservation, idempotent
merges, composite keys, rowids, sequence identities, BLOBs and missing-key
verification. No new dependencies or live AgentMesh requests are needed.
