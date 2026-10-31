# Home-server team knowledge

These routes run only in the Python home server's bundled OpenHuddle hub. The
Worker and client/MCP tools are unchanged. Use the existing active member bearer
token; the server derives the team from that membership, never from request
fields. Missing, invalid, removed-member and former viewer credentials return
401. A valid member cannot read or write another team's data.

## Task handoffs

`POST /api/handoff` accepts:

```json
{
  "task_key": "T1",
  "summary": "Implementation ready for review",
  "changed_files": ["server/src/example.py"],
  "open_questions": ["Which release should include this?"],
  "next_step": "Run the verification suite"
}
```

Only the task's creator or current assignee may write (403 for other members,
including an unrelated team owner). The task must exist in the caller's team
(404 otherwise). Summary is required; summary and next step each allow at most
8,000 UTF-16 code units. Changed files and open questions are arrays of strings;
omitted arrays default to empty and an omitted next step defaults to empty.
The existing hub request-size limit applies. Task keys are normalized uppercase.

The response is `{handoff: {...}}`. `GET /api/handoffs?task_key=T1` returns
`{handoffs: [...]}` to any member of that team, ordered by creation time descending
and then id descending. Omit `task_key` to read the whole team's handoff history.
Each entry includes `id`, `task_key`, `author_id`, `summary`, `changed_files`,
`open_questions`, `next_step` and `created_at` (Unix milliseconds). An entry is
append-only: there is no update or delete route. A `handoff_created` event records
the structured handoff in the team's permanent feed. History remains readable
even if an explicit plan replacement removes its task.
Task keys with handoff history remain reserved within that team. A replacement
plan requesting such a key receives a fresh key through the existing allocator;
archived handoffs cannot silently attach to an unrelated new task. Use the task
key returned by `/api/plan`, as with other occupied keys.

## Shared notes

| Method and route | Request / response |
| --- | --- |
| `POST /api/notes` | `{title, body, tags: []}` → `{note: {...}}` |
| `GET /api/notes?q=&tag=` | `{notes: [...]}`, newest first, id breaking time ties |
| `DELETE /api/notes/<id>` | Author only; `{ok: true}` after physical row deletion |

Titles and bodies must be nonempty strings, limited to 200 and 8,000 UTF-16 code
units respectively. Tags must be an array of at most ten strings (empty if
omitted); blank tags are discarded and duplicates collapsed. Hub text cleaning
removes terminal controls and redacts recognized credentials, as on other hub
routes. Overlong or malformed values return 400 rather than being truncated.

`q` matches a literal case-insensitive substring of title, body or any tag. `tag`
matches one complete, case-sensitive tag. Both filters apply when supplied
together; SQL wildcard punctuation has no special meaning. Empty filters return
all team notes. Entries contain `id`, `author_id`, `title`, `body`, `tags` and
`created_at`. A nonauthor DELETE returns 403 even for a team owner; missing and
other-team ids return 404. GET/POST never remove a note. There is no soft-delete
column, tombstone or automatic expiration.

Creating a note records `note_created`; explicit deletion records `note_deleted`.
The note row is deleted, but permanent feed history (including the creation
event's content) is retained. This is not an erasure operation on historical
events or backups.

## Storage, dashboard and local verification

Schema migration v3 appends only the `task_handoffs` and `team_notes` tables and
their indexes. Existing tables, rows and migration steps are unchanged. Neither
new table cascades with task/member removal, and neither participates in cleanup.
Both use the existing local `hub.db`, transactions and backup procedures; restart
with the same private configuration directory, not a fresh initialization.

In member dashboard access, handoffs appear on task cards, latest first, and
Notes has search, exact tag filter, creation and explicit two-step author delete
controls. Remote text is rendered only with text nodes. Credentials are runtime
bearer headers in the existing tab-scoped session, never served in HTML.

Run the regression suite from the repository root:

```bash
(cd server && PYTHONPATH=src python3 -m pytest -q)
(cd cloudflare && npm run typecheck >/dev/null && echo worker-unchanged-typecheck-ok)
```

Tests use fresh private local configuration directories and loopback servers,
not the live API. They cover route authentication/scoping, additive upgrade,
restart/retention, literal text rendering and dashboard actions. The standalone
headless-Chrome check, when Chrome is installed, exercises the served home page:

```bash
cd server && PYTHONPATH=src python3 tests/team_features_e2e.py
```

No deployment, tunnel provisioning or systemd changes are needed.
