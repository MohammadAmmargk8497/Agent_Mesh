# Unified team dashboard

Both servers serve one self-contained, writable `dashboard.html` at `/`.
`/viewer` and `/index.html` return 302 redirects to `/`; `/t/<team-id>` remains
a deep link to the same page. On the Worker, root/index/viewer routes are
restricted to `agentmesh.zahid.win`, while team URLs work on both routed hosts.
The Python server does not restrict these pages by hostname.

## Access and panels

Enter a member key or legacy agent token in the single access form at `/`;
there is no access-mode selector or team ID field. On load, the page checks
`GET /api/state` using its browser cookie. Without a valid session it shows the
sign-in form; submitting a key sends it once to `POST /auth/login`. After a
successful cookie-backed team read, root access checks `/v1/dashboard/state` to
retain the existing seeded-agent controls when permitted; otherwise it uses the
member's team and writes as that member. The former viewer identity
and `VIEWER_TOKEN` no longer authenticate. `/v1/transcript` returns 401 without
agent authentication and remains forbidden (403) to agents.

A personal `/t/<team-id>#k=<member-key>` link explicitly requests member access,
even if the seeded key also authenticates as a legacy agent token. The fragment
is removed immediately and the key is exchanged at `/auth/login`, not retained
by the page. Credentials are never kept in JavaScript application state beyond
that request, sessionStorage, localStorage, HTML or the loaded URL. Obsolete
browser-storage credentials are purged. A key for a
different team is rejected before any team content is rendered. Sign-out aborts
pending requests and streams and clears private content, drafts and credentials.

### Persistent cookie and security trade-off

Both HTTP boundaries (home server and Worker) implement the same cookie contract
so the byte-identical dashboard remains usable on both hosts:

- `POST /auth/login` accepts `{key}`, validates it using the same team state as
  `/api/state`, and returns only `{team_id}`. It sets
  `agentmesh_session=<key>; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000`.
  A rejected key expires an existing cookie. Hub unavailability does not.
- `POST /auth/logout` expires the cookie with the same flags and `Max-Age=0`.
  It does not rotate or revoke the underlying member key.
- Cookie-authenticated `/api/*` and `/v1/*` requests receive the corresponding
  Bearer authorization only at the server boundary, never in browser JavaScript.
  An explicit Authorization header keeps the existing CLI/API behavior and takes
  precedence even if invalid. Native `/v1/*` cookie access also checks current
  membership, so removed/rotated member keys cannot linger as browser sessions.
- Every `/auth/*` POST and cookie-authenticated POST/PUT/DELETE/PATCH requires a
  same-origin Origin header; missing or cross-origin headers are rejected with
  403. Cookie-authenticated WebSocket upgrades also require same-origin Origin,
  even though their handshake is GET: WebSockets do not enforce CORS. Existing
  explicit-Bearer clients do not acquire an Origin requirement.
  Both boundaries compare the exact effective scheme, host and port. For TLS
  termination, the Python boundary trusts one literal `X-Forwarded-Proto: https`
  only from a loopback proxy; malformed/duplicate values are rejected, and remote
  clients cannot choose the effective scheme. An HTTPS reverse proxy must forward
  that header correctly. Direct HTTP is treated as HTTP, not also as HTTPS.

Cookie sign-in requires the team hub. A home server with the hub disabled keeps
its explicit-Bearer legacy API, but browser sign-in is unavailable until the hub
is enabled; the page does not invent a team session.

The **cookie contains the access key**. HttpOnly prevents page scripts from
reading it; Secure requires HTTPS (or a browser-supported localhost development
exception), and SameSite=Strict adds cross-site protection. Login survives
reload, closing/reopening a tab, and browser restarts for up to 30 days, until
Sign out, cookie removal, or loss of key validity. **Anyone with access to that
browser profile can use the workspace until Sign out.** Use a private/trusted
profile on shared machines. A fresh profile has no session and shows sign-in.
Sign-out needs a successful server request; a network failure explicitly warns
that the cookie may remain active. Only non-secret access-mode/view/section/theme
preferences are kept in localStorage. Member mode is remembered so a personal
root-fragment session retains its member panels on later visits.
Cookies are shared between tabs in the same profile. If another tab changes the
signed-in member/team, the dashboard resets old content, drafts and caches before
showing the new identity's data; an old fixed-key session must not leak across it.

The existing layout, palette, dark theme, sign-out and access form are retained.
Member access provides the copied hub behaviors in that style:

- Board: take, accept, decline, start, release and mark done; dependency readiness.
- Huddles: start, ideas/standup updates, discussions, backing/push-back votes and
  starter close-with-decision. Design/review huddles require a proposal.
- Master prompt: sections, edit/save/cancel, submit, approve/reject with comments,
  lock status and amendments preserving the previous locked version.
- Team status, active tasks/files, house rules and reusable join-link copy.
- Live feed, questions/answers and file claims/releases.
- Header connection chips from each team member's `last_seen`: online up to two
  minutes, away up to fifteen minutes, offline thereafter or when never seen.
  Names and all teammate text use text nodes; last-seen context is visible.

Team membership management and house-rule changes still use the existing CLI/API
or agent tools; this page does not invent a membership manager or team switcher.
The unchanged hub authenticates requests as activity, so viewing a dashboard
refreshes that member's presence. Chips describe membership presence, not a
separate agent-process connection.

## Browser tools on the same page

The dashboard keeps the AgentMesh palette, fonts, existing theme toggle and panel
layout. No additional API endpoints, network hosts or runtime dependencies are
needed for these controls:

- **Grouped section navigation:** the left sidebar has four headings, with group
  badges counting sections (4/3/3/3) and each entry showing its panel's live count:
  - Work: Board, Timeline, Plans / Master prompt, Claims.
  - Conversation: Transcript, Questions, Huddles.
  - Knowledge: Notes, Handoffs, Patches.
  - Team and activity: Members and presence, Activity, Team rules.
  Desktop keeps the compact panel overview and highlights/focuses the selected
  section. On phones, navigation sits above the content and only the selected
  section is shown. Activity keeps both feed and audit; Team rules keeps both the
  coordination guide and house rules. Their counts combine those panels' loaded
  records/rules. Handoffs summarizes loaded task handoffs and links to their
  original cards and actions; the original task-card handoff forms remain intact.
  Notes/handoffs without home-server member data show **Unavailable**. Only the
  selected section identifier is saved in localStorage (`agentmesh:current-section`),
  never an access key. Search result links select the relevant phone section.
- **Live pulse and working indicators:** recent member heartbeats and recorded
  activity/in-progress assignments light up member chips. The working strip names
  the agent and its task keys/titles, while stale/missing heartbeats are labelled.
  New messages/task events ripple; task status changes slide and flash on the board.
  Legacy data without heartbeats says that presence is unavailable.
- **Timeline:** Board/Timeline switches the task view to a horizontally scrollable,
  chronological track of loaded messages, task creation/latest updates, and loaded
  task events. Tasks without timestamps are counted as unavailable, not given an
  invented time. It is not a complete task-status history.
- **Theme polish:** light/dark surfaces, text and borders transition smoothly.
  Motion effects respect the browser's reduced-motion setting.
- **Global search:** the single search box matches loaded tasks, messages,
  questions and huddles plus home-server notes/handoffs when those are available.
  Result links reveal and focus the item, including tasks hidden by a filter.
  Search does not fetch history; notes-panel filters limit the loaded notes.
  Unavailable home-only data is explicitly labelled.
- **Notifications:** off by default for every session, even if browser permission
  was previously granted. **Enable notifications** is the only permission prompt;
  a supported secure browser is required. Only newly observed questions addressed
  to the authenticated identity and new assignments/proposals for it notify.
  Existing records and unchanged polls do not notify. Disable with the same button;
  signing out clears this tab's opt-in. Notification bodies can contain work text.
- **Task filters and saved views:** All tasks, My open tasks, Blocked (including
  unfinished dependencies), and Done today. Done today uses a recorded completion
  timestamp or loaded `task_done` event, never a generic update time; completed
  tasks missing that evidence are excluded with a count. Dates use browser-local
  time. Up to twelve named filters can be saved/deleted in localStorage; only the
  view name and filter value are saved, never searches, work data or access keys.
- **Keyboard:** `/` focuses search; `j`/`k` move through visible cards (including
  handoffs, and timeline cards in timeline mode); `[`/`]` move to the first section
  in the previous/next group, wrapping at the ends; `?` opens shortcut help. Typing
  in inputs/editable fields and modifier-key combinations are not intercepted.
  Escape closes help or leaves search.
- **Export:** transcript/board buttons download Markdown generated solely from
  the currently loaded snapshot, preserving complete task keys. Board export
  includes all loaded tasks, not just filtered cards. These are not full backups.
- **Phones:** panels stack, toolbar controls wrap, and only the timeline track
  scrolls horizontally; the page itself stays within the viewport.

## Home-server handoffs and notes

The Python home server additionally shows **Handoffs** on each team task card and
a **Notes** panel in member access. These extensions are not available on the
Worker or in legacy agent access. For seeded credentials that default to legacy
access at `/`, use the personal `/t/<team-id>#k=<member-key>` link to open member
access. No token is embedded in a served page.

Task creators and current assignees can save a summary, changed files, open
questions and next step on the card. Any team member can read all handoffs,
newest first. The Notes panel supports creating a title/body with up to ten
comma-separated tags, case-insensitive text search, and an exact tag filter;
search and tag filters combine. Only a note's author sees its delete action.
Deleting requires **Delete note**, then **Confirm delete**; cancellation changes
nothing. Confirmation sends a real `DELETE`, removing the row, not hiding it.

Both features use local `hub.db` and survive a server restart. Handoffs and notes
are never automatically pruned, including after task replacement or member
removal. An author-requested note deletion is the explicit exception; its feed
history remains. Structured task handoffs are also recorded in the feed. See
[the API and storage contract](team-notes.md).

The shared static dashboard templates remain byte-identical across hosts. The
Python boundary composes these server-only controls into the served template
inside the existing member session. A changed insertion point fails explicitly
rather than silently dropping controls. All teammate text uses text nodes;
sign-out clears notes, handoffs, drafts and confirmation state and aborts reads.
Idle polls reuse cached knowledge while the team's event cursor is unchanged;
changing only the note filters reloads notes, not handoff history.

## Legacy compatibility and isolation

Live transcript, Message activity and Patches remain on the same page. Member
access reads their full team data through a read-only host adapter, using the
same member key:

| GET route | Result |
| --- | --- |
| `/api/legacy/transcript?team=<id>&after=<cursor>` | Legacy snapshot, including messages, audit and patch metadata |
| `/api/legacy/patches?team=<id>` | Legacy patch metadata |
| `/api/legacy/patches/<uuid>?team=<id>` | One legacy patch |

The caller must be an active member of the originally seeded default team, and
`team` must match that membership. Other teams, even if named `default`, cannot
read the shared legacy room. Those teams see an explanatory empty state in the
legacy panels; their hub panels continue working. Writes through the adapter are
rejected. Legacy records are not copied into hub history.

Python verifies immutable seed-ledger metadata with a read-only SQLite connection.
The Worker caches the seeded team ID verified through existing hub authentication.
Normal seeded-agent traffic populates the cache; a generated member key arriving
first after a cold activation requires one seeded-identity lookup, which can
refresh that seeded member's presence once. Subsequent adapter reads use the cache.

Legacy agent access retains its `/v1` permissions, writable controls and
four-second snapshots on the same page. Private messages, questions and cursors
remain scoped to that agent. Member access uses the unchanged writable `/api/*`
hub calls and authenticated SSE, with periodic snapshot recovery. The adapter
only reads legacy records; visible member actions write to the hub, not the
legacy room. The question panel combines pending asks with questions and
answers observed during the session; trimming the latest-150-event feed does not
remove open questions. A new session begins from pending asks and recent events,
not a complete historical question archive.

## Verification

From the repository root:

```bash
(cd cloudflare && npm run typecheck)
(cd cloudflare && npm test)
(cd server && PYTHONPATH=src python3 -m pytest -q)
(cd server && PYTHONPATH=src python3 -m pytest -q tests/test_dashboard_polish.py)
(cd cloudflare && npx wrangler deploy --dry-run --outdir /tmp/one-view-dryrun >/dev/null 2>&1 && echo dry-run-ok)
```

The new unified-dashboard tests execute the real inline scripts against both
local hub implementations without additional dependencies. They complement the
existing dashboard tests and cover member/agent actions, rejected former viewer
credentials, redirects, presence thresholds, text safety, team isolation and
byte-identical page copies. No live deployment is needed.

The real headless Chrome polish test serves a representative local home-data
snapshot with the nonce CSP, blocks non-loopback requests, and verifies search
links, downloaded Markdown task keys, keyboard focus, chronological timeline,
filters/views, notification opt-in/deduplication, literal text, HttpOnly cookie
flags/lifetime, reload and new-tab persistence, a fresh browser profile,
rejected-session cleanup, sign-out and absence of credentials in browser storage,
page HTML and API Authorization headers. All thirteen section links,
their panel counts, group counts, bracket shortcuts, section persistence and
phone single-section visibility are checked at both 1440px and 390px, including
page overflow and navigation positioning. It needs
an already installed Chrome/Chromium and Node, but adds no browser dependencies.
Run pytest invocations sequentially: the configured `.pytest-tmp` is shared, so
concurrent invocations can remove each other's active fixture directories. Use
distinct `--basetemp` paths if independent test runs must overlap.
