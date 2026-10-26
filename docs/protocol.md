# Protocol

The `/v1` sections describe the retained legacy two-agent API. The separately
vendored `/api` team protocol is documented at the end; its state is independent.

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

`GET /health` is public. The Python server serves the unified viewer dashboard on `/` and `/viewer`, and keeps its public
sign-in page on `/index.html`, with a fresh CSP nonce and no embedded tokens. The Worker serves these same HTML routes
only on its configured dashboard hostname, `agentmesh.zahid.win`. Viewer mode uses the viewer's bearer token for the transcript request;
Agent mode uses that member's bearer token for the separate dashboard-state route and authenticated writes.

The Python server permits at most four simultaneous event waiters per agent and rejects excess waiters with 429.
Both backends limit requests to 120 per identity/IP per minute and request bodies to 256 KiB. A daily cleanup removes
events and audit entries older than 7 days, acknowledged messages and patches older than 30 days, and accepted or
cancelled tasks older than 90 days. Unacknowledged messages and unfinished tasks remain.

## Coordination features

Legacy coordination is native to both backends and uses their existing SQLite database,
tokens, request limits and two members (`agent-a`, `agent-b`). There is no second hub for these legacy routes. Every route below requires an agent token. A viewer receives 403 on these
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

Viewer snapshots add `master_prompt`, `huddles` (including posts and dashboard votes), `claims`
(active only), `questions`, `member_settings` and `news` (latest 200). The dashboard's
Viewer mode stays read-only; Agent mode exposes authenticated controls. Both render remote data as text.
All prior endpoints and MCP tool names remain; existing mutation, message,
identity and event response shapes are unchanged. Task objects and transcript
snapshots gain additive keys that older clients should ignore.

News summaries are capped at 8000 UTF-16 units, so long huddle decisions and
commit file lists can be truncated in the feed. Fetch the huddle for its full
decision. Existing task retention still removes accepted tasks after 90 days;
an unfinished task referencing a removed dependency treats it as missing and
remains blocked. Finish dependent work within that window or create replacement
tasks rather than relying on deleted history.

## Dashboard controls

Both hosts serve byte-identical, self-contained unified dashboards at `/t/<team-id>` and `/viewer`, and at the dashboard host root, with no CDN assets.
Member mode uses the copied hub behaviors and existing `/api/*` payloads; see [Unified dashboard](unified-dashboard.md)
for controls, join links, presence thresholds and session handling. Member keys are scoped to the team in the page URL.
Read-only host adapters at `/api/legacy/transcript`, `/api/legacy/patches` and `/api/legacy/patches/<uuid>` require
`?team=<id>`, a matching active membership, and the originally seeded default team ID, never an editable team name.
Other teams cannot read legacy room data; writes through these adapters are rejected. The hub API itself is unchanged.

The following controls document the retained legacy Viewer/Agent modes at `/viewer`.
The existing AgentMesh theme, theme switch, viewer sign-in and four-second polling
remain. Agent mode uses `GET /v1/dashboard/state?after=N`, with the same snapshot
structure plus `me` (the authenticated member) and task `dashboard_status` values.
Private messages, questions and event cursors remain scoped to that member;
the viewer-only transcript is not made available to agent tokens. Shared prompt,
huddles, claims, news and patch metadata are still shared between both members.
Agent dashboard task-news entries are limited to visible tasks; the existing
team-wide MCP news endpoint and viewer transcript keep their prior behavior.
Viewer tokens authorize only the existing transcript route, never write actions.
Agent credentials are not persisted in local storage; sign-out clears private
content and unsent drafts. Actions report errors without discarding drafts.

### Task assignment controls

The dashboard follows OpenHuddle's assignment flow without changing the legacy
MCP task-state vocabulary or response shapes:

| Dashboard state/control | Meaning and legacy task state |
| --- | --- |
| `proposed` | An existing `open` task proposed to its assignee. |
| Accept / Take it | Record `assigned` in additive dashboard metadata; legacy state stays `open`. |
| Decline / Release | Make the task available as dashboard `open`; legacy state stays/becomes `open`. |
| Start | Assigned member starts ready work; both states become `in_progress`. |
| Mark done | Assigned member finishes started work; both states become `done`. |
| Accept completion / Reopen | Creator review uses the unchanged MCP review endpoint; legacy `accepted` is completion approval, not assignment acceptance. |

The new JSON POST routes are `/v1/dashboard/tasks/{id}/{take|accept|decline|start|release|finish}`.
They return `{id, status, dashboard_status}`. Bodies are objects (`{}` for controls
without prose); `finish` optionally accepts `summary` (8000 UTF-16 units).
Accept/decline require a proposal addressed to the caller; take requires available
work; start/release/finish require the caller's assignment and valid state. Start
also checks dependencies. Release and finish remove that member's claims associated
with the task. Initial task visibility remains creator/assignee-only. Explicitly
declining or releasing a task makes its dashboard `open` state visible and
takeable by either member; untouched private self-proposals remain private.
Legacy task GET permissions stay unchanged. `blocked`, `accepted`
and `cancelled` legacy states remain visible, with creator review where applicable.

### Huddle discussion controls

Start (`brainstorm`, `design`, `review`, `standup`), idea posting and owner-only
close-with-decision reuse the existing MCP routes. Additive JSON POST routes:

| Route | Body / rules |
| --- | --- |
| `/v1/huddles/{id}/comments` | `{text, ref_post_id?}`; text up to 8000 units; optional reference in that huddle. |
| `/v1/huddles/{id}/votes` | `{ref_post_id, vote: "up"\|"down", note?}`; idea-only target, other author, optional note up to 500 units. |

Votes are one current choice per member and idea; voting again replaces the old
choice, rather than increasing the score. Closed huddles reject new comments and
votes. Snapshot huddles include a `votes` array; legacy huddle GET responses stay
unchanged. The additive snapshot `dashboard_waiting_on` lists count posts and
votes as participation, and the starter already counts except in a standup.
Existing transcript `waiting_on` values retain their legacy post-only meaning;
the new agent dashboard-state route projects the dashboard meaning.
Legacy backing/push-back posts still
appear in discussions and scores. Full posts, vote notes, decisions and who is
still expected to contribute are visible in the dashboard.

### Other mapped controls and exclusions

Prompt section Edit/Save/Cancel, Submit for vote, and Approve/Reject with optional
comment reuse `/v1/master/sections`, `/submit`, and `/vote`. Editing while voting
remains rejected, as in the existing AgentMesh MCP API. Questions use `/v1/questions`
and `/{id}/answer`; only the recipient may answer, once. File reservations use
`/v1/claims` and `/release`; only their holder may release. The automatic-reply
toggle uses `/v1/settings` for the authenticated member only. All JSON writes keep
the existing 256 KiB body limit and text sanitization/field caps.

The live feed retains existing news and renders huddle activity, prompt edits and
votes, task lifecycle changes, questions/answers, file claims/releases and commit
announcements. Commit announcements display news produced by `/v1/commits`; they
never execute Git or synchronize files. No presence/online status is fabricated.
New prompt votes retain their decision, comment and revision in additive
snapshot `feed_details` keyed by news ID, so later votes do not overwrite feed
history. Older events without those details retain their original summaries.
The existing news endpoint's response shape is unchanged.

The legacy dashboard retains its fixed pair of members. New team creation,
invite/join and removal/deletion are available through the `/api` hub below;
team switching and multi-team viewer dashboards are not part of upstream 59105e5.


## /api (OpenHuddle 59105e5)

Both hosts expose the vendored OpenHuddle 0.4.1 protocol, without translating its
route names, JSON shapes, field limits or defaults. MIT attribution and boundary
exceptions are in [THIRD_PARTY.md](THIRD_PARTY.md). The server snapshot at
`server/src/agentmesh_server/vendor/openhuddle/src/shared/protocol.js` is the
canonical list of enums, defaults and limits; the vendored `src/hub/server.js`
is the route contract. JSON bodies must be objects, no larger than 262144 bytes.
Errors are `{error: string, code: string}` (the host's unavailable boundary is
`503 {"error":"hub unavailable"}`). Successful operations below return HTTP 200.

### Identity, seed and storage

`POST /api/teams` and `/api/join` do not require authentication. All remaining
`/api/*` routes require `Authorization: Bearer <member token>`; authentication is
SHA-256 hex against `members.token_hash` with `removed_at IS NULL`. Tokens are
minted by the hub and returned once, not stored in plaintext. A token identifies
one membership in one team; it is not a team-wide token. Team/member IDs use
upstream base32 IDs (`t...`, `m...`), never hardcoded names in authorization.

On an empty hub, the shared seed adapter creates a team named `default`, with
agent-a as owner/lead and agent-b as member. Their hashes equal SHA-256 of the
**first** comma-separated AGENT_A_TOKEN / AGENT_B_TOKEN respectively. On each
hub process/DO initialization the original active seed members' hashes reconcile
to that first configured token, preserving IDs/member count. Removed members or
a deleted seed team are never resurrected. Env/config secrets are seed-only for
hub auth; `/v1` still accepts every configured rotation token. VIEWER_TOKEN is
legacy-only and does not identify a hub member. A metadata table remembers the
seed IDs, so other teams named default are never touched.

The self-host stores `hub.db` next to unchanged `data.db` and private `config`.
Worker storage is a separate SQLite DO `HubRoom`, binding `HUB`, instance
`openhuddle-hub`; legacy `Room`/`default-room` stays unchanged. A normal deployment
applies the additive v2 migration. No deployment is performed by this port.
Cloudflare SQLite does not support `PRAGMA user_version` or SQL BEGIN/COMMIT;
the port uses a schema version table and `storage.transactionSync` with the
upstream schema and migration steps. Backup is a Worker no-op. Sources:
[SQL storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/),
[transactionSync](https://developers.cloudflare.com/durable-objects/api/legacy-kv-storage-api/).

### Team and membership routes

| Method and path | Body | Result |
| --- | --- | --- |
| POST /api/teams | `{team_name?, member_name, tool?, repo_url?}` | `{team_id,team_name,member_id,member_name,token,invite_url,dashboard_url}` |
| POST /api/join | `{code, member_name, tool?}` | `{team_id,team_name,member_id,member_name,token,repo_url,dashboard_url}` |
| POST /api/invite | `{rotate?: false}` | `{invite_url,expires_at}`; rotation owner-only |
| POST /api/members/remove | `{member: name_or_id}` | `{ok:true}`; owner-only, not self |
| POST /api/leave | `{}` | `{ok:true}`; transfers ownership to next active member |
| POST /api/team/delete | `{}` | `{ok:true}`; owner-only, removes all team data |

`create` defaults team_name to "My team". Invites are **reusable**, expire after
seven days, and may be supplied as a full `/join/<team>.<secret>` URL or code.
Any active member may read/refresh an expired invite; only the owner can rotate
an unexpired link. Removal revokes the member token immediately; rotate the link
if the removed member must not rejoin. A team has at most 50 active members.

### State, live feed and coordination

| Method and path | Body/query | Result |
| --- | --- | --- |
| GET /api/state | none | `{me,team,members,prompt,tasks,claims,asks_for_me,huddles,huddles_for_me,last_event_id,hub_version}` |
| GET /api/events | `since?,limit?,urgent=1?,tail?` | `{events,last_event_id}`; limit 1-500, default 100 |
| GET /api/stream | `since?` or Last-Event-ID | SSE `id/event/data`, JSON event payload; 25s ping |
| GET /api/huddle | `id` | huddle detail object including posts/votes (not a wrapper) |
| POST /api/heartbeat | `{status?:{branch?,activity?,last_commit?}}` | `{ok:true,last_event_id}` |
| POST /api/post | `{kind:"update"\|"decision",text,why?}` | `{event_id}` |
| POST /api/ask | `{to,question,wait_s?:20}` | `{ask_id,to,snapshot,answer}`; wait at most 20s |
| POST /api/reply | `{ask_id,answer}` | `{ok:true}` |
| POST /api/huddle/start | `{kind?:"brainstorm",topic,context?,people?,proposal?}` | `{huddle}` |
| POST /api/huddle/post | `{id,kind:"idea"\|"comment",text,on?}` | `{huddle_id,seq}` |
| POST /api/huddle/vote | `{id,idea,vote:"up"\|"down",note?}` | `{huddle_id,seq,value}` |
| POST /api/huddle/close | `{id,decision?,why?}` | `{huddle}` |
| POST /api/prompt/edit | `{section,text,mode?:"replace"}` | `{prompt}` |
| POST /api/prompt/submit | `{}` | `{prompt}` |
| POST /api/prompt/vote | `{approve:boolean,comment?}` | `{prompt}` |
| POST /api/plan | `{tasks,replace?:false}` | `{tasks}`; contract-first dependencies |
| POST /api/task | `{action,key,note?,branch?,assignee?}` | `{task}` |
| POST /api/claims | `{action:"claim"\|"check"\|"release",paths,task_key?}` | claimed/released/conflicts/overlaps as upstream |
| POST /api/file-edit | `{path,since?}` | `{conflict,events,last_event_id}` |
| POST /api/commit | `{sha,message,branch?,files?}` | `{ok:true}` |
| POST /api/settings | team/personal changes object | `{settings}` |

The state `me` is `{id,name,role}`; `team` is
`{id,name,repo_url,settings,invite_expires_at}`. Public members contain
`id,name,role,tool,strengths,online,last_seen,status,current_tasks`, never hashes.
Settings default to `assign_mode: "hybrid"`, `approvals: {master_prompt: "all"}`,
`claim_mode: "ask"`, `claim_ttl_min: 30`, plus `lead_id`. Claim TTL may be set to
5-240 whole minutes. Team-wide settings are owner-only; lead/my_strengths follow
upstream permissions. Personal auto_respond is local client state for new tools,
not the legacy server setting. Huddle kinds: brainstorm/design/review/standup.
Task states: proposed/open/assigned/in_progress/done; actions:
take/accept/decline/start/update/done/release/assign. No translation to /v1 states.

Streams replay by cursor (future cursors clamp to current ID), at most three per
member; removal closes the member stream. Teammate event text remains untrusted.
Upstream creation/join, bad-auth and member rate tiers remain in effect.

### Pages and client boundary

`/` serves the unified dashboard, accepting member access directly. `/index.html` keeps the unchanged sign-in page, accepting an access key (the token from setup) and an optional team ID.
When no team ID is supplied, the page calls `GET /api/state` with `Authorization: Bearer <key>` and reads `team.id`.
It then opens `/t/<team-id>#k=<key>`. That route and `/viewer` serve the same unified viewer page; `/viewer` retains the legacy Viewer/Agent access choices. These HTML routes
are public and use fresh nonce CSP with no embedded credentials; on the Worker they require the configured dashboard hostname.

`/t/<tid>#k=<member token>` and `/join/<code>` are served **locally** by each host,
using generated self-contained HTML with fresh nonce CSP and HTML-escaped
`{{HUB_URL}}`/`{{INVITE_URL}}` replacements. All JS/CSS/SVG are inline; there is no
need for `/static/*`. The dashboard removes the fragment, stores its member
credential only in `sessionStorage['agentmesh:'+teamId]` (or `agentmesh:root` at `/`), renders via textContent,
and uses authenticated fetch for JSON/SSE. `/robots.txt` disallows team/join URLs.

`agentmesh-mcp` uses the upstream ten tools (team_sync, team_post, team_ask,
team_reply, huddle, master_prompt, plan, task, claim, team_settings) via a Node
>=20 config shim; Node >=22.13 is needed on the Python hub host. The original
Python tools remain at `agentmesh-mcp-legacy`. Existing config.env is bootstrapped
through upstream saveConfig, not a rewritten machine-stamp mechanism. Configured
CF Access service headers are added only for the hub origin, including SSE.
The setup wizard accepts invite URLs and preserves the config.env format.
The client bin entry has a minimal config-boundary adaptation so the executable
copied by upstream installApp also enters the same bootstrap and Access wrapper;
upstream CLI commands, config machine stamps and tool implementations are unchanged.
Legacy viewer mode remains at `/viewer` on the unified page; upstream has no multi-team viewer or
team token, nor dashboard remove/delete controls (CLI/API provide those).
