# Protocol

Task states: `open -> in_progress -> done | blocked -> accepted | open (reopened)`; the creator may `cancel` any task that isn't closed.
Only the assignee may claim/complete; only the creator may review/cancel. Tasks are visible only to their creator and assignee.

Every state change, message, and patch writes an event for the other agent. Clients read events by cursor
(`GET /v1/events?after=N&wait=S`, long-poll up to 25s) or receive them live on the `/v1/connect` WebSocket.
Events are notifications only; fetch the referenced object for its content.

Patch records contain UTF-8 text (unified diffs or review notes); binary files are out of scope. Listing returns metadata only.

Authentication: the Worker maps each bearer token to an agent identity (constant-time compare), strips the token,
and forwards the identity in an internal header. The Durable Object is never exposed directly.
