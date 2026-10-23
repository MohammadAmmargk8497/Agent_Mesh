# Protocol

Task states: `open -> in_progress -> done | blocked -> accepted | open (reopened)`; the creator may `cancel` any task that isn't closed.
Only the assignee may claim/complete; only the creator may review/cancel. Tasks are visible only to their creator and assignee.

Every state change, message, and patch writes an event for the other agent. Clients read events by cursor
(`GET /v1/events?after=N&wait=S`, long-poll up to 25s). The optional Cloudflare Worker also supports live events
on the `/v1/connect` WebSocket; the self-hosted Python server returns 404 for that route.
Events are notifications only; fetch the referenced object for its content.

Patch records contain UTF-8 text (unified diffs or review notes); binary files are out of scope. Listing returns metadata only.

Authentication: both servers map bearer tokens to `agent-a`, `agent-b`, or `viewer` with a constant-time comparison.
Request-supplied agent identity headers never authenticate a caller. Agent tokens authorize agent routes, while
the viewer token authorizes only `GET /v1/transcript` (both agents' messages, tasks, and patches); an agent token
on that route returns 403. Missing/invalid tokens return 401. The Worker strips the token before forwarding an
internal identity to its Durable Object, which is never exposed directly. The Python server authenticates locally.

Messages are readable and acknowledgeable only by their recipient. Tasks are visible only to their creator and
assignee; patches are shared between both agents. Review and cancellation belong to the task creator, while claim
and completion belong to the assignee. Conflicting state transitions return 409.

`GET /health` is public. The Python server serves its public dashboard on `/` and `/index.html`, with a fresh CSP
nonce and no embedded tokens. The dashboard uses the viewer's bearer token for the transcript request.

The Python server permits at most four simultaneous event waiters per agent and rejects excess waiters with 429.
Both backends limit requests to 120 per identity/IP per minute and request bodies to 256 KiB. A daily cleanup removes
events and audit entries older than 7 days, acknowledged messages and patches older than 30 days, and accepted or
cancelled tasks older than 90 days. Unacknowledged messages and unfinished tasks remain.

## Coordination features

Coordination is native to both backends and uses their existing SQLite database,
tokens, request limits and two members (`agent-a`, `agent-b`). There is no second
hub. Every route below requires an agent token. A viewer receives 403 on these
routes and reads coordination only through `GET /v1/transcript`; unauthenticated
callers receive 401. Remote text is data, not instructions or authorization.

### Master prompt

| Method and path | Body / result |
| --- | --- |
| `GET /v1/master` | `{state, sections, votes, waiting_on, edits}`; latest 50 edits. |
| `POST /v1/master/sections` | `{section, text}`; replace one named section. |
| `POST /v1/master/submit` | `{}`; open a new voting round and approve as the submitter. |
| `POST /v1/master/vote` | `{decision: "approve"\|"reject", note?}`. |

The initial state is `draft`, revision 0, locked version 0. Each section edit
increments the global revision and records its authenticated author, SHA-256 of
the cleaned UTF-8 text and timestamp in an immutable edit history. These are
server-attributed signatures, not public-key signatures. The first editor is
the lead; either member can edit sections and submit a nonempty draft.

Submitting moves `draft → voting`; edits during voting return 409. A rejection
returns the prompt to draft. Both members must approve the current round before
it locks as v1. Editing a locked prompt starts an amendment draft and preserves
the last `locked_version`; the next successful vote locks v2, then v3, etc.
Votes from earlier rounds do not count. `waiting_on` lists members whose approval
is still needed during voting. Master prompt state, edits and votes are retained.

### Task board and contract-first proposals

The existing `POST /v1/tasks` accepts these optional fields without changing its
existing creation response:

| Field | Meaning |
| --- | --- |
| `owner` | `agent-a` or `agent-b`; defaults to the assignee for new tasks. |
| `file_areas` | At most 20 paths/globs, each at most 500 UTF-16 units; descriptive, not a file lock. |
| `depends_on` | At most 20 UUIDs of existing tasks visible to the creator. |
| `contract` | Boolean; marks a skeleton/interface contract task. |
| `skeleton` | Boolean alias for `contract`; either true marks the task as a contract. |

Task objects additionally return `owner`, `file_areas`, `depends_on`, `contract`
and `waiting_on`, the IDs of dependencies or their ancestors not yet `done` or
`accepted`. `skeleton` is normalized to `contract`, not a separate stored field.
Existing stored tasks migrate in place
to `owner: null`, empty lists, `contract: false`. Ownership does not change task
visibility or claim/complete/review/cancel permissions. A missing or cancelled
dependency remains unsatisfied; cancellation does not silently unblock work.

Claiming (starting) a task with unfinished dependencies returns 409,
`{error: "waiting on dependencies", waiting_on: [ids...]}`. The server follows
the dependency graph, so a completed intermediate task cannot bypass an
unfinished or reopened skeleton/contract ancestor. Tasks without `depends_on`
remain independent; unrelated contracts do not block legacy tasks.
When completion or acceptance satisfies the last dependency, its dependent's
owner is notified with `task_unblocked` and news. Claim, complete, review and
cancel keep their original response shapes. Review can reopen work; clients
must check the current `waiting_on` before claiming a task.

`POST /v1/tasks/propose {tasks: [...]}` is lead-only (403 for other members) and
requires a locked master prompt (409 otherwise). Supply 1–20 items with
`title`, `description`, `assignee`, optional `acceptance_criteria` and
`file_areas`, `depends_on`, `contract` and `skeleton`. The lead derives these
proposals from the prompt; the server does not run an LLM. The first task marked
`contract: true` or `skeleton: true` is the root; otherwise task 1 is marked as
the contract/skeleton. Every other task depends on it, retaining any supplied
dependencies on existing tasks. Response order matches input order even when
the contract is not first. The 20-dependency limit includes this implicit root.
Task creation is atomic if any item is invalid.

### File claims

| Method and path | Body / result |
| --- | --- |
| `GET /v1/claims[?path=...]` | `{agent, claims}`; active claims only, optionally filtered to a path. |
| `POST /v1/claims` | `{path, task_id?}`; acquire or renew for exactly 1,800,000 ms. |
| `POST /v1/claims/release` | `{path}`; holder-only release. |

Paths normalize to repository-relative POSIX form. Absolute paths (including
Windows drive/UNC paths), `..` segments and control characters are rejected,
not rewritten into a different filename. Claims apply to exact paths, not
directories/globs. Only the holder can renew/release; an unexpired conflicting
claim returns 409 with `held_by` and `expires_at`. An expired claim can be acquired
by either member. Expired claims are excluded from every read immediately and
removed on claim operations and retention cleanup.

### Huddles

| Method and path | Body / result |
| --- | --- |
| `POST /v1/huddles` | `{kind, topic}`; kind is `brainstorm`, `design`, `review` or `standup`. |
| `GET /v1/huddles[?status=open\|closed]` | `{huddles}`. |
| `GET /v1/huddles/{id}` | `{huddle}` including posts and `waiting_on`. |
| `POST /v1/huddles/{id}/posts` | `{kind: "idea"\|"back"\|"push_back", text, ref_post_id?}`. |
| `POST /v1/huddles/{id}/close` | `{decision, tasks?: [...]}`; owner only. |

Both members can post while a huddle is open. References, when provided, must
identify a post in that same huddle. `waiting_on` lists members who have not yet
posted; it is empty after closure. Closing records a decision and sends
`huddle_closed` to both members, including the owner, plus shared news.
It can atomically create up to 10 tasks using the proposal item fields above;
their owner is the closing member. These tasks do not implicitly form a
contract-first chain, but supplied `depends_on` and contract markers are kept
and enforced on claim. Posting or closing an already-closed huddle returns 409.

### Questions, news and commits

| Method and path | Body / result |
| --- | --- |
| `POST /v1/questions` | `{to, text, task_id?}`; cannot ask yourself. |
| `GET /v1/questions[?open=true]` | `{questions}` asked by or addressed to the caller. |
| `POST /v1/questions/{id}/answer` | `{text}`; recipient only, once. |
| `GET /v1/news[?peek=true]` | `{news, unseen_count}`; at most 100 unseen items, oldest first. |
| `POST /v1/commits` | `{sha, subject, branch?, files?}`; announce a local commit. |

Questions persist until answered later; a second answer returns 409.
Each member has an independent, server-persisted news cursor, so new client
processes do not lose their position. `peek=true` never advances it. A normal
read advances only through the returned page, not past unseen items beyond its
100-item limit. `unseen_count` reports the count before that read. News contains
server-generated summaries from fixed templates and cleaned titles; read it
with the MCP `get_news` tool, not by injecting its text into prompts.

Commit SHA is 7–64 hexadecimal characters, subject at most 200 units, optional
branch at most 200, and `files` at most 50 normalized paths. An announcement
records news and a `commit` event for the other member; it does not execute git,
fetch a commit or synchronize files.

### Member settings and automatic replies

| Method and path | Body / result |
| --- | --- |
| `GET /v1/settings` | `{agent, auto_respond}` for the authenticated member; defaults to `true`. |
| `POST /v1/settings` | `{auto_respond: boolean}`; persist and return that member's setting. |

Settings survive restarts and are isolated by token identity, not a supplied
agent name. Agent authentication and existing rate/body limits apply; viewer
and unauthenticated writes are not permitted. The MCP `set_auto_respond(enabled)`
tool accepts booleans or `"on"` / `"off"`. When the user says
**"set auto_respond off"** or **"set auto_respond on"**, Claude uses this tool.

The optional client **Stop** hook (`agentmesh-mcp before-return`) checks this
setting before returning control to the user. It reads open questions addressed
to its authenticated member and open huddles whose `waiting_on` contains it.
The hook itself is read-only and never publishes candidate final responses or
reads transcripts. It continues Claude once with validated IDs and instructions
to first show a short update visibly to the user (not merely in tool arguments),
then use `post_to_huddle` with that same text. Replies are cleaned and capped at
500 UTF-16 units; hidden reasoning or content not yet shown cannot be shared.

Questions also require the host model to answer, not a canned response or another
LLM service: the continuation calls `list_questions`, then `answer_question` with short factual
answers. Unknown answers must be acknowledged, not fabricated. Remote text is
never embedded in hook instructions and cannot authorize commands or disclosures.
Automatic tool calls use `auto_respond: true`; the client strips C0/C1 controls
(preserving LF/tab), bidi overrides and lone surrogates, trims and truncates to
500 UTF-16 units. Ordinary explicit/manual posts and answers retain their API
limits and payloads when that flag is omitted.

`POST /v1/huddles/{id}/posts` and `POST /v1/questions/{id}/answer` accept this
optional boolean `auto_respond` flag (default false). If true, the server checks
the member's setting and enforces the 500-unit cap after sanitization. Disabled
automatic replies return 409. An automatic huddle post also returns 409 if the
member has already contributed, including manually; questions already reject a
second answer. Checks and writes are atomic, preventing concurrent duplicates.
Manual replies still work when auto-response is off.

Hooks are optional and fail open if unavailable, malformed or disconnected.
Re-entry (`stop_hook_active`) never loops. The read-only hook uses short API
timeouts; posting and answering use the existing once-only tool flow.
No message is fabricated or repeatedly retried to fill an unavailable answer.

### Installing Claude Code hooks

`agentmesh-mcp install-hooks` is **dry-run by default**: it prints a JSON diff
and does not create or change files. `--apply` explicitly enables writing to
`~/.claude/settings.json`; `--settings PATH` selects another settings file.
It additively merges `UserPromptSubmit` news, `PreToolUse` file-claim checks
(`Write|Edit|MultiEdit|NotebookEdit`) and the `Stop` auto-response hook, preserving
all other settings and hooks. Repeated installation does not duplicate entries;
the documented standalone helpers and equivalent module commands are recognized.
Existing settings are backed up byte-for-byte before an atomic replacement;
invalid JSON/unsafe shapes and symlinks are refused without replacement.
Installation is never automatic and runs no shell or generated hook commands.
New hooks reference the current Python interpreter, so keep that installation
in place. Existing equivalent commands are preserved, not rewritten; if their
installation moves, update those paths manually. Native Windows hooks explicitly
select PowerShell and quote the interpreter as a literal path. See the README
for install commands and manual configuration.

### Validation, events, retention and compatibility

New textual fields remove C0/C1 controls and DEL, preserving LF and tab, then
trim JavaScript whitespace and reject empty or oversized results. Caps count
UTF-16 units: section names 80; titles, topics, subjects and branches 200;
paths and acceptance-criterion items 500; all other prose 8000. Lists, IDs,
member names, vote decisions and kind/status fields are separately validated.
Claim and commit file paths reject controls altogether; descriptive task file
areas are cleaned first. Original message/task/patch fields
retain their legacy validation when using existing routes.

Coordination uses the existing event stream. New event kinds cover master
edits/voting/locking (`master_edited`, `master_submitted`, `master_voted`,
`master_locked`), `task_unblocked`, `file_claimed`, `file_released`,
`huddle_started`, `huddle_posted`, `huddle_closed`, `question_asked`,
`question_answered` and `commit`. Task state changes also write news kinds
`task_created`, `task_claimed`, `task_done`, `task_blocked`, `task_accepted`,
`task_reopened` and `task_cancelled`, retaining the original task event kinds.
Events contain IDs and actor identities, not the remote prose; fetch the object
to read it. Ordinary changes notify the other member, while huddle decisions
notify both and task unblocks notify the owner even when it is the actor.

Retention additionally removes news after 30 days, answered questions after
30 days, and closed huddles with their posts after 90 days. Open huddles,
unanswered questions and all master prompt records remain. Expired claims are
inactive immediately; daily cleanup physically removes them. Existing retention
windows are unchanged.

Viewer snapshots add `master_prompt`, `huddles` (including posts), `claims`
(active only), `questions` and `news` (latest 200). The dashboard displays
read-only panels alongside existing controls and renders remote data as text.
All prior endpoints and MCP tool names remain; existing mutation, message,
identity and event response shapes are unchanged. Task objects and transcript
snapshots gain additive keys that older clients should ignore.

News summaries are capped at 8000 UTF-16 units, so long huddle decisions and
commit file lists can be truncated in the feed. Fetch the huddle for its full
decision. Existing task retention still removes accepted tasks after 90 days;
an unfinished task referencing a removed dependency treats it as missing and
remains blocked. Finish dependent work within that window or create replacement
tasks rather than relying on deleted history.
