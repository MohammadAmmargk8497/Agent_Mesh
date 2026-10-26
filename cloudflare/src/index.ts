import { DurableObject } from "cloudflare:workers";
import dashboard from "./dashboard.html";
import signin from "./signin.html";
import hubDashboard from "./hub_dashboard.html";
import hubJoin from "./hub_join.html";
export { HubRoom } from "./openhuddle/hub-room";
import type { HubRoom } from "./openhuddle/hub-room";
import { Coordination, HttpError } from "./coordination";

interface Env {
  ROOM: DurableObjectNamespace<Room>;
  HUB: DurableObjectNamespace<HubRoom>;
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
const legacyTeams = new WeakMap<Env, string>();
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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

// Legacy data belongs only to the seeded default team. Resolve that identity
// through the existing hub API, never by the editable/non-unique team name.
async function legacyAdapter(request: Request, env: Env, ip: string): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.slice("/api/legacy".length);
  if (request.method !== "GET") return error("legacy panels are read-only", 405);
  if (path !== "/transcript" && path !== "/patches" && !/^\/patches\/[0-9a-f-]{36}$/i.test(path)) return error("not found", 404);
  const hub = env.HUB.get(env.HUB.idFromName("openhuddle-hub"));
  const stateFor = (token: string) => hub.fetch(new Request(url.origin + "/api/state", {
    headers: { authorization: "Bearer " + token, "x-agentmesh-client-ip": ip },
  }));
  const authorization = request.headers.get("authorization") ?? "";
  const token = authorization.startsWith("Bearer ") ? authorization.slice(7).trim() : "";
  const response = await stateFor(token);
  if (!response.ok) return response;
  const state = await response.json() as any;
  const team = url.searchParams.get("team");
  if (!team || team !== state.team?.id) return error("team does not match member key", 403);
  let defaultTeam = legacyTeams.get(env);
  if (!defaultTeam) {
    const agent = await identify(token, env);
    if ((agent === "agent-a" || agent === "agent-b") && state.me?.name === agent) defaultTeam = state.team.id;
    else {
      for (const [name, secret] of [["agent-a", env.AGENT_A_TOKEN], ["agent-b", env.AGENT_B_TOKEN]]) {
        const seeded = await stateFor(secret.split(",")[0].trim());
        if (!seeded.ok) { await seeded.body?.cancel(); continue; }
        const seededState = await seeded.json() as any;
        if (seededState.me?.name === name) { defaultTeam = seededState.team?.id; break; }
      }
    }
    if (defaultTeam) legacyTeams.set(env, defaultTeam);
  }
  if (!defaultTeam || team !== defaultTeam) return error("legacy panels are available only for the default team", 403);
  const legacyUrl = new URL("/v1" + path, url.origin);
  if (url.searchParams.has("after")) legacyUrl.searchParams.set("after", url.searchParams.get("after")!);
  // Only this fixed read-only allowlist can enter the legacy room; caller-supplied
  // identity headers and hub credentials are never forwarded to it.
  return env.ROOM.get(env.ROOM.idFromName("default-room")).fetch(new Request(legacyUrl, {
    headers: { "x-agentmesh-agent": path === "/transcript" ? "viewer" : "agent-a" },
  }));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    // Hub member tokens live in the hub DB, not the legacy environment identify gate.
    const hubRoute = url.pathname.startsWith("/api/") || url.pathname.startsWith("/t/") || url.pathname.startsWith("/join/") || url.pathname === "/robots.txt";
    if (hubRoute) {
      const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
      if (env.LIMITER && !(await env.LIMITER.limit({ key: `ip:${ip}` })).success) return error("rate limited", 429);
      if (url.pathname.startsWith("/api/legacy/")) return legacyAdapter(request, env, ip);
      if (url.pathname.startsWith("/api/")) {
        const headers = new Headers(request.headers);
        headers.set("x-agentmesh-client-ip", ip); // Never trust a caller-supplied limiter identity.
        const response = await env.HUB.get(env.HUB.idFromName("openhuddle-hub")).fetch(new Request(request, { headers }));
        // Learn the default identity from real agent traffic where possible, rather
        // than issuing a service-side lookup that refreshes an agent's presence.
        if (url.pathname === "/api/state" && request.method === "GET" && response.ok && !legacyTeams.has(env)) {
          const authorization = request.headers.get("authorization") ?? "";
          const agent = await identify(authorization.startsWith("Bearer ") ? authorization.slice(7).trim() : "", env);
          if (agent === "agent-a" || agent === "agent-b") {
            const state = await response.clone().json() as any;
            if (state.me?.name === agent && typeof state.team?.id === "string") legacyTeams.set(env, state.team.id);
          }
        }
        return response;
      }
      const head = request.method === "HEAD";
      if (request.method !== "GET" && !head) return error("not found", 404);
      if (url.pathname === "/robots.txt") return new Response(head ? null : "User-agent: *\nDisallow: /t/\nDisallow: /join/\n", { headers: { "content-type": "text/plain; charset=utf-8" } });
      const join = /^\/join\/([a-z0-9]+\.[A-Za-z0-9_-]+)$/.exec(url.pathname);
      if (!join && !/^\/t\/[a-z0-9]+$/.test(url.pathname)) return error("not found", 404);
      const escapeHtml = (value: string) => value.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
      const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
      const page = (join ? hubJoin : dashboard).replaceAll("{{HUB_URL}}", escapeHtml(url.origin))
        .replaceAll("{{INVITE_URL}}", escapeHtml(url.origin + url.pathname)).replaceAll("__AGENTMESH_NONCE__", nonce);
      return new Response(head ? null : page, { headers: {
        "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer", "x-frame-options": "DENY",
        "content-security-policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'`,
      } });
    }
    const dashboardHost = url.hostname === "agentmesh.zahid.win";
    if (dashboardHost && (request.method === "GET" || request.method === "HEAD") &&
        (url.pathname === "/" || url.pathname === "/index.html" || url.pathname === "/viewer")) {
      const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
      const page = url.pathname === "/index.html" ? signin : dashboard;
      return new Response(request.method === "HEAD" ? null : page.replaceAll("__AGENTMESH_NONCE__", nonce), { headers: {
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
  private coordination: Coordination;
  private deferredPushes: Array<() => void> | null = null;

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
    this.coordination = new Coordination((query, ...args) => this.sql(query, ...args),
      (recipient, actor, type, ref, includeSelf) => this.emit(recipient, actor, type, ref, includeSelf),
      callback => this.atomic(callback));
    ctx.blockConcurrencyWhile(async () => {
      if ((await ctx.storage.getAlarm()) === null) await ctx.storage.setAlarm(Date.now() + DAY);
    });
  }

  private sql(query: string, ...args: unknown[]) { return this.ctx.storage.sql.exec(query, ...args); }

  private atomic<T>(callback: () => T): T {
    const pushes: Array<() => void> = [];
    this.deferredPushes = pushes;
    let result: T;
    try { result = this.ctx.storage.transactionSync(callback); }
    finally { this.deferredPushes = null; }
    for (const push of pushes) push();
    return result;
  }

  // Records a durable event for the recipient, then pushes it to live WebSockets and long-pollers.
  private emit(recipient: string, actor: Agent, type: string, ref: string, includeSelf = false) {
    if (recipient === actor && !includeSelf) return;
    const created_at = Date.now();
    const id = this.sql("INSERT INTO events (recipient, type, ref, actor, created_at) VALUES (?,?,?,?,?) RETURNING id",
      recipient, type, ref, actor, created_at).one().id;
    const event = JSON.stringify({ type: "event", event: { id, type, ref, actor, created_at } });
    const push = () => {
      for (const ws of this.ctx.getWebSockets(recipient)) { try { ws.send(event); } catch {} }
      for (const wake of this.waiters) wake();
      this.waiters.clear();
    };
    if (this.deferredPushes) this.deferredPushes.push(push);
    else push();
  }

  private lastEventId(agent: Agent): number {
    return Number(this.sql("SELECT COALESCE(MAX(id), 0) AS id FROM events WHERE recipient=?", agent).one().id);
  }

  private task(id: string, agent: Agent): any {
    const t = this.sql("SELECT * FROM tasks WHERE id=?", id).toArray()[0] as any;
    if (!t || (t.assignee !== agent && t.created_by !== agent)) throw new HttpError(404, "task not found");
    return this.coordination.parseTask(t);
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
      const dashboardState = path === "/v1/dashboard/state" && method === "GET";
      if (agent === "viewer" || dashboardState) {
        if (agent === "viewer" && (path !== "/v1/transcript" || method !== "GET")) return error("viewer is read-only", 403);
        if (agent !== "viewer" && !AGENTS.includes(agent)) return error("invalid agent context", 401);
        const rawAfter = url.searchParams.get("after");
        const after = Number(rawAfter ?? 0);
        if ((rawAfter !== null && !/^-?\d+$/.test(rawAfter)) || !Number.isSafeInteger(after))
          return error("after must be an integer");
        const events = dashboardState
          ? this.sql("SELECT * FROM events WHERE recipient=? AND id>? ORDER BY id ASC LIMIT 200", agent, after).toArray()
          : this.sql("SELECT * FROM events WHERE id>? ORDER BY id ASC LIMIT 200", after).toArray();
        const tasks = (dashboardState
          ? this.sql("SELECT * FROM tasks WHERE created_by=? OR assignee=? OR (status='open' AND EXISTS (SELECT 1 FROM dashboard_tasks WHERE task_id=tasks.id AND state='open')) ORDER BY created_at DESC LIMIT 200", agent, agent)
          : this.sql("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200")).toArray().reverse().map((r: any) => {
            const task = this.coordination.parseTask(r);
            if (dashboardState) task.dashboard_status = this.coordination.dashboardStatus(task);
            return task;
          });
        const snapshot = this.coordination.snapshot(Date.now(), agent === "viewer" ? undefined : agent);
        return json({
          messages: (dashboardState
            ? this.sql("SELECT * FROM messages WHERE sender=? OR recipient=? ORDER BY created_at DESC LIMIT 500", agent, agent)
            : this.sql("SELECT * FROM messages ORDER BY created_at DESC LIMIT 500")).toArray().reverse(),
          tasks,
          patches: this.sql("SELECT id, sender, title, task_id, created_at, length(text) AS size FROM patches ORDER BY created_at DESC LIMIT 50").toArray().reverse(),
          audit: (dashboardState
            ? this.sql("SELECT * FROM audit WHERE actor=? ORDER BY id DESC LIMIT 200", agent)
            : this.sql("SELECT * FROM audit ORDER BY id DESC LIMIT 200")).toArray().reverse(),
          events,
          last_event_id: events.length ? events[events.length - 1].id : after,
          ...snapshot,
          ...(dashboardState ? { me: agent } : {}),
        });
      }
      if (!AGENTS.includes(agent)) return error("invalid agent context", 401);
      if (path === "/v1/transcript") return error("viewer token required", 403);
      const now = Date.now();
      // Additive authenticated routes: /v1/master, /v1/huddles, /v1/claims, /v1/news.
      const coordinationResponse = await this.coordination.route(request, agent, path, method, url, now);
      if (coordinationResponse) return coordinationResponse;

      if (path === "/v1/whoami" && method === "GET") return json({ agent });

      if (path === "/v1/inbox" && method === "GET") {
        const parse = (r: any) => this.coordination.parseTask(r);
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
        return json({ tasks: rows.map((r: any) => this.coordination.parseTask(r)) });
      }
      if (path === "/v1/tasks" && method === "POST") {
        const b = await readJson(request);
        return this.atomic(() => {
          if (!validString(b.title, 200) || !validString(b.description, 8000)) return error("title and description are required");
          if (!AGENTS.includes(b.assignee)) return error("assignee must be agent-a or agent-b");
          const criteria = b.acceptance_criteria ?? [];
          if (!Array.isArray(criteria) || criteria.length > 20 || criteria.some((x: unknown) => !validString(x, 500))) return error("acceptance_criteria must contain at most 20 strings");
          const extras = this.coordination.taskExtras(b, agent);
          const id = crypto.randomUUID();
          this.sql(
            `INSERT INTO tasks (id, title, description, assignee, created_by, status, acceptance_criteria, result, created_at, updated_at, owner, file_areas, depends_on, contract)
             VALUES (?,?,?,?,?,'open',?,NULL,?,?,?,?,?,?)`,
            id, b.title.trim(), b.description.trim(), b.assignee, agent, JSON.stringify(criteria), now, now,
            extras.owner, JSON.stringify(extras.file_areas), JSON.stringify(extras.depends_on), Number(extras.contract));
          this.emit(b.assignee, agent, "task_assigned", id);
          this.coordination.taskNews(agent, "task_created", id, now);
          return json({ id, status: "open", created_by: agent }, 201);
        });
      }

      const tm = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})(?:\/(claim|complete|review|cancel))?$/i);
      if (tm) {
        const id = tm[1], action = tm[2];
        const task = this.task(id, agent);
        if (method === "GET" && !action) return json({ task });
        if (method !== "POST") return error("method not allowed", 405);

        if (action === "claim") {
          // Claim and cancel deliberately ignore their request body, as older clients expect.
          return this.atomic(() => {
            const task = this.task(id, agent);
            if (task.assignee !== agent) return error("only assignee may claim", 403);
            if (task.status !== "open") return error("task is not open", 409);
            if (task.waiting_on.length) return json({ error: "waiting on dependencies", waiting_on: task.waiting_on }, 409);
            this.setStatus(id, "in_progress", now);
            this.emit(task.created_by, agent, "task_claimed", id);
            this.coordination.taskNews(agent, "task_claimed", id, now);
            return json({ id, status: "in_progress" });
          });
        }
        if (action === "complete") {
          if (task.assignee !== agent) return error("only assignee may complete", 403);
          if (task.status !== "in_progress") return error("claim the task before completing it", 409);
          const b = await readJson(request);
          return this.atomic(() => {
            // Parsing can yield to another request; validate the current row, not the earlier snapshot.
            const task = this.task(id, agent);
            if (task.assignee !== agent) return error("only assignee may complete", 403);
            if (task.status !== "in_progress") return error("claim the task before completing it", 409);
            if (!validString(b.summary, 8000)) return error("summary is required");
            if (b.status !== undefined && b.status !== "done" && b.status !== "blocked") return error("status must be done or blocked");
            const status = b.status ?? "done";
            this.setStatus(id, status, now, b.summary);
            this.emit(task.created_by, agent, `task_${status}`, id);
            this.coordination.taskNews(agent, `task_${status}`, id, now);
            if (status === "done") this.coordination.taskUnblocked(id, agent, now);
            return json({ id, status });
          });
        }
        if (action === "review") {
          if (task.created_by !== agent) return error("only the creator may review", 403);
          if (task.status !== "done" && task.status !== "blocked") return error("task is not awaiting review", 409);
          const b = await readJson(request);
          return this.atomic(() => {
            const task = this.task(id, agent);
            if (task.created_by !== agent) return error("only the creator may review", 403);
            if (task.status !== "done" && task.status !== "blocked") return error("task is not awaiting review", 409);
            if (b.decision !== "accept" && b.decision !== "reopen") return error("decision must be accept or reopen");
            if (b.note !== undefined && !validString(b.note, 8000)) return error("note must be a non-empty string");
            const status = b.decision === "accept" ? "accepted" : "open";
            this.setStatus(id, status, now);
            if (b.note && task.assignee !== agent) {
              this.sql("INSERT INTO messages (id, sender, recipient, text, task_id, created_at, acked_at) VALUES (?,?,?,?,?,?,NULL)",
                crypto.randomUUID(), agent, task.assignee, `[review: ${b.decision}] ${b.note}`, id, now);
            }
            this.emit(task.assignee, agent, status === "accepted" ? "task_accepted" : "task_reopened", id);
            this.coordination.taskNews(agent, status === "accepted" ? "task_accepted" : "task_reopened", id, now);
            if (status === "accepted" && task.status !== "done") this.coordination.taskUnblocked(id, agent, now);
            return json({ id, status });
          });
        }
        if (action === "cancel") {
          return this.atomic(() => {
            const task = this.task(id, agent);
            if (task.created_by !== agent) return error("only the creator may cancel", 403);
            if (task.status === "accepted" || task.status === "cancelled") return error("task is already closed", 409);
            this.setStatus(id, "cancelled", now);
            this.emit(task.assignee, agent, "task_cancelled", id);
            this.coordination.taskNews(agent, "task_cancelled", id, now);
            return json({ id, status: "cancelled" });
          });
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
    this.coordination.sweep(now);
    await this.ctx.storage.setAlarm(now + DAY);
  }
}
