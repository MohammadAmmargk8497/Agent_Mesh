import { DurableObject } from "cloudflare:workers";
import dashboard from "./dashboard.html";

interface Env {
  ROOM: DurableObjectNamespace<Room>;
  // Comma-separated to allow rotation: "new,old". Each token must be >= 32 chars.
  AGENT_A_TOKEN: string;
  AGENT_B_TOKEN: string;
  VIEWER_TOKEN?: string;
  LIMITER?: RateLimit;
}
type Agent = "agent-a" | "agent-b";
const AGENTS: Agent[] = ["agent-a", "agent-b"];
const MAX_BODY = 256 * 1024;
const MAX_PATCH = 200 * 1024;
const MAX_WAIT_S = 25;
const MAX_SOCKETS_PER_AGENT = 4;
const DAY = 86_400_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: {
    "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
    "x-content-type-options": "nosniff"
  }});
}
function error(message: string, status = 400) { return json({ error: message }, status); }
async function readJson(request: Request): Promise<any> {
  if (Number(request.headers.get("content-length") ?? "0") > MAX_BODY) throw new HttpError(413, "request body too large");
  const buf = await request.arrayBuffer();
  if (buf.byteLength > MAX_BODY) throw new HttpError(413, "request body too large");
  let b: unknown;
  try { b = JSON.parse(new TextDecoder().decode(buf)); } catch { throw new HttpError(400, "invalid JSON"); }
  if (!b || typeof b !== "object" || Array.isArray(b)) throw new HttpError(400, "body must be a JSON object");
  return b;
}
function validString(v: unknown, max = 4000): v is string {
  return typeof v === "string" && v.trim().length > 0 && v.length <= max;
}
function optionalId(v: unknown): string | null {
  if (v == null || v === "") return null;
  if (typeof v !== "string" || !UUID.test(v)) throw new HttpError(400, "task_id must be a UUID");
  return v;
}

const sha256 = (s: string) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
// Constant-time comparison of hashes so response timing doesn't leak token bytes.
async function identify(token: string, env: Env): Promise<Agent | "viewer" | null> {
  const presented = await sha256(token);
  for (const [agent, secret] of [["agent-a", env.AGENT_A_TOKEN], ["agent-b", env.AGENT_B_TOKEN], ["viewer", env.VIEWER_TOKEN]] as const) {
    for (const candidate of (secret ?? "").split(",").map(s => s.trim())) {
      if (candidate.length >= 32 && crypto.subtle.timingSafeEqual(presented, await sha256(candidate))) return agent;
    }
  }
  return null;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const dashboardHost = url.hostname === "agentmesh.zahid.win";
    if (dashboardHost && request.method === "GET" &&
        (url.pathname === "/" || url.pathname === "/index.html")) {
      const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
      return new Response(dashboard.replaceAll("__AGENTMESH_NONCE__", nonce), { headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "content-security-policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'`,
      }});
    }

    const header = request.headers.get("authorization") ?? "";
    const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
    const agent = token ? await identify(token, env) : null;
    if (agent === "viewer" && (url.pathname !== "/v1/transcript" || request.method !== "GET"))
      return error("viewer is read-only", 403);
    if (url.pathname === "/health" && request.method === "GET")
      return json({ ok: true, service: "agentmesh" });
    // Unauthenticated callers are limited per IP (slows token guessing / abuse); agents per identity.
    const limitKey = agent ?? `ip:${request.headers.get("cf-connecting-ip") ?? "unknown"}`;
    if (env.LIMITER && !(await env.LIMITER.limit({ key: limitKey })).success) return error("rate limited", 429);
    if (!agent) return error("unauthorized", 401);

    const headers = new Headers(request.headers);
    headers.delete("authorization"); // the Durable Object never needs the raw token
    headers.set("x-agentmesh-agent", agent); // overwrites any client-supplied value
    return env.ROOM.get(env.ROOM.idFromName("default-room")).fetch(new Request(request, { headers }));
  }
};

export class Room extends DurableObject<Env> {
  private waiters = new Set<() => void>();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec(`
      CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
        assignee TEXT NOT NULL, created_by TEXT NOT NULL, status TEXT NOT NULL,
        acceptance_criteria TEXT NOT NULL, result TEXT, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
        text TEXT NOT NULL, task_id TEXT, created_at INTEGER NOT NULL, acked_at INTEGER
      );
      CREATE TABLE IF NOT EXISTS patches (
        id TEXT PRIMARY KEY, sender TEXT NOT NULL, title TEXT NOT NULL,
        text TEXT NOT NULL, task_id TEXT, created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL, type TEXT NOT NULL,
        ref TEXT NOT NULL, actor TEXT NOT NULL, created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
        ref TEXT NOT NULL, created_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS events_recipient ON events(recipient, id);
    `);
    ctx.blockConcurrencyWhile(async () => {
      if ((await ctx.storage.getAlarm()) === null) await ctx.storage.setAlarm(Date.now() + DAY);
    });
  }

  private sql(query: string, ...args: unknown[]) { return this.ctx.storage.sql.exec(query, ...args); }

  // Records a durable event for the recipient, then pushes it to live WebSockets and long-pollers.
  private emit(recipient: string, actor: Agent, type: string, ref: string) {
    if (recipient === actor) return;
    const created_at = Date.now();
    const id = this.sql("INSERT INTO events (recipient, type, ref, actor, created_at) VALUES (?,?,?,?,?) RETURNING id",
      recipient, type, ref, actor, created_at).one().id;
    const event = JSON.stringify({ type: "event", event: { id, type, ref, actor, created_at } });
    for (const ws of this.ctx.getWebSockets(recipient)) { try { ws.send(event); } catch {} }
    for (const wake of this.waiters) wake();
    this.waiters.clear();
  }

  private lastEventId(agent: Agent): number {
    return Number(this.sql("SELECT COALESCE(MAX(id), 0) AS id FROM events WHERE recipient=?", agent).one().id);
  }

  private task(id: string, agent: Agent): any {
    const t = this.sql("SELECT * FROM tasks WHERE id=?", id).toArray()[0] as any;
    if (!t || (t.assignee !== agent && t.created_by !== agent)) throw new HttpError(404, "task not found");
    return { ...t, acceptance_criteria: JSON.parse(String(t.acceptance_criteria)) };
  }

  private setStatus(id: string, status: string, now: number, result?: string) {
    if (result === undefined) this.sql("UPDATE tasks SET status=?, updated_at=? WHERE id=?", status, now, id);
    else this.sql("UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=?", status, result, now, id);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const method = request.method.toUpperCase();
    const agent = request.headers.get("x-agentmesh-agent") as Agent | "viewer";

    try {
      // Viewer snapshots never enter agent helpers, mutation paths, or WebSocket handling.
      if (agent === "viewer") {
        if (path !== "/v1/transcript" || method !== "GET") return error("viewer is read-only", 403);
        const rawAfter = url.searchParams.get("after");
        const after = Number(rawAfter ?? 0);
        if ((rawAfter !== null && !/^-?\d+$/.test(rawAfter)) || !Number.isSafeInteger(after))
          return error("after must be an integer");
        const events = this.sql("SELECT * FROM events WHERE id>? ORDER BY id ASC LIMIT 200", after).toArray();
        return json({
          messages: this.sql("SELECT * FROM messages ORDER BY created_at DESC LIMIT 500").toArray().reverse(),
          tasks: this.sql("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200").toArray().reverse()
            .map((r: any) => ({ ...r, acceptance_criteria: JSON.parse(String(r.acceptance_criteria)) })),
          patches: this.sql("SELECT id, sender, title, task_id, created_at, length(text) AS size FROM patches ORDER BY created_at DESC LIMIT 50").toArray().reverse(),
          audit: this.sql("SELECT * FROM audit ORDER BY id DESC LIMIT 200").toArray().reverse(),
          events,
          last_event_id: events.length ? events[events.length - 1].id : after,
        });
      }
      if (!AGENTS.includes(agent)) return error("invalid agent context", 401);
      if (path === "/v1/transcript") return error("viewer token required", 403);
      const now = Date.now();

      if (path === "/v1/whoami" && method === "GET") return json({ agent });

      if (path === "/v1/inbox" && method === "GET") {
        const parse = (r: any) => ({ ...r, acceptance_criteria: JSON.parse(String(r.acceptance_criteria)) });
        return json({
          agent,
          unread_messages: this.sql("SELECT * FROM messages WHERE recipient=? AND acked_at IS NULL ORDER BY created_at ASC LIMIT 100", agent).toArray(),
          assigned_tasks: this.sql("SELECT * FROM tasks WHERE assignee=? AND status IN ('open','in_progress') ORDER BY created_at ASC LIMIT 100", agent).toArray().map(parse),
          tasks_to_review: this.sql("SELECT * FROM tasks WHERE created_by=? AND assignee<>? AND status IN ('done','blocked') ORDER BY updated_at ASC LIMIT 100", agent, agent).toArray().map(parse),
          last_event_id: this.lastEventId(agent),
        });
      }

      // Long-poll: returns events after `after`, waiting up to `wait` seconds if there are none yet.
      if (path === "/v1/events" && method === "GET") {
        const after = url.searchParams.has("after") ? Number(url.searchParams.get("after")) : this.lastEventId(agent);
        const wait = Math.min(Math.max(Number(url.searchParams.get("wait") ?? 0), 0), MAX_WAIT_S);
        if (!Number.isInteger(after) || Number.isNaN(wait)) return error("after must be an integer and wait a number");
        const read = () => this.sql("SELECT * FROM events WHERE recipient=? AND id>? ORDER BY id ASC LIMIT 100", agent, after).toArray();
        let events = read();
        if (!events.length && wait > 0) {
          await new Promise<void>(resolve => {
            const timer = setTimeout(done, wait * 1000);
            function done() { clearTimeout(timer); resolve(); }
            this.waiters.add(done);
          });
          events = read();
        }
        return json({ events, last_event_id: events.length ? events[events.length - 1].id : after });
      }

      if (path === "/v1/tasks" && method === "GET") {
        const status = url.searchParams.get("status");
        const rows = this.sql(
          `SELECT * FROM tasks WHERE (assignee = ? OR created_by = ?) ${status ? "AND status = ?" : ""} ORDER BY created_at DESC LIMIT 100`,
          agent, agent, ...(status ? [status] : [])).toArray();
        return json({ tasks: rows.map((r: any) => ({ ...r, acceptance_criteria: JSON.parse(String(r.acceptance_criteria)) })) });
      }
      if (path === "/v1/tasks" && method === "POST") {
        const b = await readJson(request);
        if (!validString(b.title, 200) || !validString(b.description, 8000)) return error("title and description are required");
        if (!AGENTS.includes(b.assignee)) return error("assignee must be agent-a or agent-b");
        const criteria = b.acceptance_criteria ?? [];
        if (!Array.isArray(criteria) || criteria.length > 20 || criteria.some((x: unknown) => !validString(x, 500))) return error("acceptance_criteria must contain at most 20 strings");
        const id = crypto.randomUUID();
        this.sql(
          `INSERT INTO tasks (id, title, description, assignee, created_by, status, acceptance_criteria, result, created_at, updated_at)
           VALUES (?,?,?,?,?,'open',?,NULL,?,?)`,
          id, b.title.trim(), b.description.trim(), b.assignee, agent, JSON.stringify(criteria), now, now);
        this.emit(b.assignee, agent, "task_assigned", id);
        return json({ id, status: "open", created_by: agent }, 201);
      }

      const tm = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})(?:\/(claim|complete|review|cancel))?$/i);
      if (tm) {
        const id = tm[1], action = tm[2];
        const task = this.task(id, agent);
        if (method === "GET" && !action) return json({ task });
        if (method !== "POST") return error("method not allowed", 405);

        if (action === "claim") {
          if (task.assignee !== agent) return error("only assignee may claim", 403);
          if (task.status !== "open") return error("task is not open", 409);
          this.setStatus(id, "in_progress", now);
          this.emit(task.created_by, agent, "task_claimed", id);
          return json({ id, status: "in_progress" });
        }
        if (action === "complete") {
          if (task.assignee !== agent) return error("only assignee may complete", 403);
          if (task.status !== "in_progress") return error("claim the task before completing it", 409);
          const b = await readJson(request);
          if (!validString(b.summary, 8000)) return error("summary is required");
          if (b.status !== undefined && b.status !== "done" && b.status !== "blocked") return error("status must be done or blocked");
          const status = b.status ?? "done";
          this.setStatus(id, status, now, b.summary);
          this.emit(task.created_by, agent, `task_${status}`, id);
          return json({ id, status });
        }
        if (action === "review") {
          if (task.created_by !== agent) return error("only the creator may review", 403);
          if (task.status !== "done" && task.status !== "blocked") return error("task is not awaiting review", 409);
          const b = await readJson(request);
          if (b.decision !== "accept" && b.decision !== "reopen") return error("decision must be accept or reopen");
          if (b.note !== undefined && !validString(b.note, 8000)) return error("note must be a non-empty string");
          const status = b.decision === "accept" ? "accepted" : "open";
          this.setStatus(id, status, now);
          if (b.note && task.assignee !== agent) {
            this.sql("INSERT INTO messages (id, sender, recipient, text, task_id, created_at, acked_at) VALUES (?,?,?,?,?,?,NULL)",
              crypto.randomUUID(), agent, task.assignee, `[review: ${b.decision}] ${b.note}`, id, now);
          }
          this.emit(task.assignee, agent, status === "accepted" ? "task_accepted" : "task_reopened", id);
          return json({ id, status });
        }
        if (action === "cancel") {
          if (task.created_by !== agent) return error("only the creator may cancel", 403);
          if (task.status === "accepted" || task.status === "cancelled") return error("task is already closed", 409);
          this.setStatus(id, "cancelled", now);
          this.emit(task.assignee, agent, "task_cancelled", id);
          return json({ id, status: "cancelled" });
        }
      }

      if (path === "/v1/messages" && method === "GET") {
        const unread = url.searchParams.get("unread") === "true";
        const rows = this.sql(
          `SELECT * FROM messages WHERE recipient=? ${unread ? "AND acked_at IS NULL" : ""} ORDER BY created_at DESC LIMIT 100`, agent).toArray();
        return json({ messages: rows.reverse() });
      }
      if (path === "/v1/messages" && method === "POST") {
        const b = await readJson(request);
        if (!AGENTS.includes(b.to)) return error("to must be agent-a or agent-b");
        if (b.to === agent) return error("cannot message yourself");
        if (!validString(b.text, 8000)) return error("text is required");
        const id = crypto.randomUUID();
        this.sql("INSERT INTO messages (id, sender, recipient, text, task_id, created_at, acked_at) VALUES (?,?,?,?,?,?,NULL)",
          id, agent, b.to, b.text, optionalId(b.task_id), now);
        this.sql("INSERT INTO audit (actor, action, ref, created_at) VALUES (?,?,?,?)", agent, "message.send", id, now);
        this.emit(b.to, agent, "message", id);
        return json({ id, created_at: now }, 201);
      }
      const am = path.match(/^\/v1\/messages\/([0-9a-f-]{36})\/ack$/i);
      if (am && method === "POST") {
        const result = this.sql("UPDATE messages SET acked_at=? WHERE id=? AND recipient=? AND acked_at IS NULL", now, am[1], agent);
        return json({ ok: true, changed: result.rowsWritten });
      }

      // Listing returns metadata only so a big patch set can't flood the agent's context window.
      if (path === "/v1/patches" && method === "GET")
        return json({ patches: this.sql("SELECT id, sender, title, task_id, created_at, length(text) AS size FROM patches ORDER BY created_at DESC LIMIT 50").toArray() });
      const pm = path.match(/^\/v1\/patches\/([0-9a-f-]{36})$/i);
      if (pm && method === "GET") {
        const patch = this.sql("SELECT * FROM patches WHERE id=?", pm[1]).toArray()[0];
        return patch ? json({ patch }) : error("patch not found", 404);
      }
      if (path === "/v1/patches" && method === "POST") {
        const b = await readJson(request);
        if (!validString(b.title, 200) || !validString(b.text, MAX_PATCH)) return error("title and text required; text max is 200 KiB");
        const id = crypto.randomUUID();
        this.sql("INSERT INTO patches (id, sender, title, text, task_id, created_at) VALUES (?,?,?,?,?,?)",
          id, agent, b.title, b.text, optionalId(b.task_id), now);
        for (const other of AGENTS) this.emit(other, agent, "patch", id);
        return json({ id, created_at: now }, 201);
      }

      if (path === "/v1/connect" && request.headers.get("Upgrade")?.toLowerCase() === "websocket") {
        if (this.ctx.getWebSockets(agent).length >= MAX_SOCKETS_PER_AGENT) return error("too many open connections", 429);
        const pair = new WebSocketPair();
        const client = pair[0], server = pair[1];
        this.ctx.acceptWebSocket(server, [agent]);
        server.serializeAttachment({ agent, connectedAt: now });
        server.send(JSON.stringify({ type: "ready", agent, at: now, last_event_id: this.lastEventId(agent) }));
        return new Response(null, { status: 101, webSocket: client });
      }
      return error("not found", 404);
    } catch (e) {
      if (e instanceof HttpError) return error(e.message, e.status);
      console.error(e); // don't leak internal/SQL details to callers
      return error("internal error", 500);
    }
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer) {
    const state = ws.deserializeAttachment() as { agent: Agent };
    const text = typeof message === "string" ? message : new TextDecoder().decode(message);
    if (text.length > 4096) { ws.close(1009, "message too large"); return; }
    let b: any;
    try { b = JSON.parse(text); } catch { ws.send(JSON.stringify({ type: "error", error: "invalid JSON" })); return; }
    if (b?.type === "ping") ws.send(JSON.stringify({ type: "pong", at: Date.now(), agent: state.agent }));
    else ws.send(JSON.stringify({ type: "info", message: "Push-only channel. Use the HTTP API to write." }));
  }

  // Daily retention sweep so storage can't grow without bound.
  async alarm() {
    const now = Date.now();
    this.sql("DELETE FROM events WHERE created_at < ?", now - 7 * DAY);
    this.sql("DELETE FROM audit WHERE created_at < ?", now - 7 * DAY);
    this.sql("DELETE FROM messages WHERE acked_at IS NOT NULL AND acked_at < ?", now - 30 * DAY);
    this.sql("DELETE FROM patches WHERE created_at < ?", now - 30 * DAY);
    this.sql("DELETE FROM tasks WHERE status IN ('accepted','cancelled') AND updated_at < ?", now - 90 * DAY);
    await this.ctx.storage.setAlarm(now + DAY);
  }
}
