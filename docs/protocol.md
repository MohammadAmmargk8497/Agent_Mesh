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
