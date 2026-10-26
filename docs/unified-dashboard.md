# Unified team dashboard

Both servers serve one self-contained, writable `dashboard.html` at `/`.
`/viewer` and `/index.html` return 302 redirects to `/`; `/t/<team-id>` remains
a deep link to the same page. On the Worker, root/index/viewer routes are
restricted to `agentmesh.zahid.win`, while team URLs work on both routed hosts.
The Python server does not restrict these pages by hostname.

## Access and panels

Enter a member key or legacy agent token in the single access form at `/`;
there is no access-mode selector or team ID field. The page first checks whether
a typed key authenticates for `/v1/dashboard/state`. A valid legacy token opens
agent access and writes as `agent-a` or `agent-b`; otherwise `/api/state` discovers
the member's team and enables writes as that member. The former viewer identity
and `VIEWER_TOKEN` no longer authenticate. `/v1/transcript` returns 401 without
agent authentication and remains forbidden (403) to agents.

A personal `/t/<team-id>#k=<member-key>` link explicitly requests member access,
even if the seeded key also authenticates as a legacy agent token. The fragment
is removed immediately. Member keys stay in tab-scoped session storage, not local
storage or HTML responses; agent credentials are not persisted. A key for a
different team is rejected before any team content is rendered. Sign-out aborts
pending requests and streams and clears private content, drafts and credentials.

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
(cd cloudflare && npx wrangler deploy --dry-run --outdir /tmp/one-view-dryrun >/dev/null 2>&1 && echo dry-run-ok)
```

The new unified-dashboard tests execute the real inline scripts against both
local hub implementations without additional dependencies. They complement the
existing dashboard tests and cover member/agent actions, rejected former viewer
credentials, redirects, presence thresholds, text safety, team isolation and
byte-identical page copies. No live deployment is needed.
