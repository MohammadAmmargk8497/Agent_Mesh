import { DurableObject } from "cloudflare:workers";
import dashboard from "./dashboard.html";
import hubDashboard from "./hub_dashboard.html";
import hubJoin from "./hub_join.html";
export { HubRoom } from "./openhuddle/hub-room";
import type { HubRoom } from "./openhuddle/hub-room";
import { Coordination, HttpError } from "./coordination";
import { EXPORT_PAGE_LIMIT, exportTables, exportPage, validExportCursor } from "./openhuddle/export";
import { AdminRecovery, IMPORT_BODY_BYTES } from "./openhuddle/admin-recovery";

interface Env {
  ROOM: DurableObjectNamespace<Room>;
  HUB: DurableObjectNamespace<HubRoom>;
  // Comma-separated to allow rotation: "new,old". Each token must be >= 32 chars.
  AGENT_A_TOKEN: string;
  AGENT_B_TOKEN: string;
  LIMITER?: RateLimit;
  VIEWER_TOKEN?: string; // Retired credentials must never authorize an export.
  ADMIN_EXPORT_TOKEN?: string;
  ADMIN_EXPORT_LIMITER?: RateLimit;
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
async function readImport(request: Request): Promise<unknown> {
  if (Number(request.headers.get("content-length") ?? "0") > IMPORT_BODY_BYTES) {
    await request.body?.cancel();
    throw new HttpError(413, "request body too large");
  }
  const reader = request.body?.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  if (reader) {
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > IMPORT_BODY_BYTES) {
          await reader.cancel();
          throw new HttpError(413, "request body too large");
        }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  let body: unknown;
  try { body = JSON.parse(new TextDecoder().decode(bytes)); }
  catch { throw new HttpError(400, "invalid JSON"); }
  if (!body || typeof body !== "object" || Array.isArray(body) ||
      Object.keys(body).length !== 1 || !Object.hasOwn(body, "rows"))
    throw new HttpError(400, "body must contain only rows");
  return (body as { rows: unknown }).rows;
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
async function identify(token: string, env: Env): Promise<Agent | null> {
  const presented = await sha256(token);
  for (const [agent, secret] of [["agent-a", env.AGENT_A_TOKEN], ["agent-b", env.AGENT_B_TOKEN]] as const) {
    for (const candidate of (secret ?? "").split(",").map(s => s.trim())) {
      if (candidate.length >= 32 && crypto.subtle.timingSafeEqual(presented, await sha256(candidate))) return agent;
    }
  }
  return null;
}

async function adminExport(request: Request, env: Env): Promise<Response> {
  if (!env.ADMIN_EXPORT_LIMITER) return error("export rate limiter unavailable", 503);
  const secret = env.ADMIN_EXPORT_TOKEN ?? "";
  if (secret.length < 32 || secret.trim() !== secret) return error("owner export is not configured", 503);
  const header = request.headers.get("authorization") ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  const hash = await sha256(token);
  const authorized = token.length >= 32 && crypto.subtle.timingSafeEqual(hash, await sha256(secret));
  try {
    const key = authorized ? "owner" : `ip:${request.headers.get("cf-connecting-ip") ?? "unknown"}`;
    if (!(await env.ADMIN_EXPORT_LIMITER.limit({ key })).success) return error("rate limited", 429);
  } catch { return error("export rate limiter unavailable", 503); }
  if (!authorized || await identify(token, env)) return error("unauthorized", 401);
  for (const viewer of (env.VIEWER_TOKEN ?? "").split(",").map(value => value.trim())) {
    if (viewer && crypto.subtle.timingSafeEqual(hash, await sha256(viewer))) return error("unauthorized", 401);
  }
  const url = new URL(request.url);
  const recovery = url.pathname === "/admin/restore" || url.pathname === "/admin/import";
  if (request.method !== (recovery ? "POST" : "GET")) return error("method not allowed", 405);
  if (!recovery && url.pathname !== "/admin/export/tables" && url.pathname !== "/admin/export/page") return error("not found", 404);
  try {
    const hub = env.HUB.get(env.HUB.idFromName("openhuddle-hub"));
    const hashHex = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("");
    // Even a misconfigured owner secret cannot elevate an existing hub member.
    // Do not call hub.auth()/fetch(): those seed/reconcile or update presence.
    if (await hub.exportTokenIsMember(hashHex)) return error("unauthorized", 401);
    const room = env.ROOM.get(env.ROOM.idFromName("default-room"));
    if (recovery) {
      const store = url.searchParams.get("store");
      if (store !== "room" && store !== "hub") return error("store must be room or hub");
      const target = store === "room" ? room : hub;
      if (url.pathname === "/admin/import") {
        const table = url.searchParams.get("table") ?? "";
        if (!table) return error("table is required");
        const imported = await target.importRows(table, await readImport(request));
        if ("error" in imported) return error(imported.error, imported.status);
        return json({ store, table, ...imported });
      }
      const rawTime = url.searchParams.get("time") ?? "";
      const dryRun = url.searchParams.get("dry_run");
      if (!/^\d+$/.test(rawTime) || !Number.isSafeInteger(Number(rawTime)) || Number(rawTime) > Date.now())
        return error("time must be a past epoch millisecond integer");
      if (dryRun !== "0" && dryRun !== "1") return error("dry_run must be 1 or 0");
      const restored = await target.prepareRestore(Number(rawTime), dryRun === "1");
      if (dryRun === "0") {
        // Scheduling already succeeded. ctx.abort() intentionally disconnects
        // this second RPC; never hide errors from lookup or restore scheduling.
        // Until the restart, exports/imports reject to prevent readiness races.
        try { await target.abortRestore(); } catch { /* expected reset disconnect */ }
      }
      return json({ store, ...restored });
    }
    if (url.pathname === "/admin/export/tables") {
      const [roomTables, hubTables] = await Promise.all([room.exportTables(), hub.exportTables()]);
      return json({ tables: [
        ...roomTables.map(table => ({ store: "room", ...table })),
        ...hubTables.map(table => ({ store: "hub", ...table })),
      ] });
    }
    const store = url.searchParams.get("store");
    const table = url.searchParams.get("table") ?? "";
    const after = url.searchParams.get("after");
    const rawLimit = url.searchParams.get("limit") ?? "100";
    if (store !== "room" && store !== "hub") return error("store must be room or hub");
    if (!table || !validExportCursor(after)) return error("invalid table or after cursor");
    if (!/^\d+$/.test(rawLimit) || !Number.isSafeInteger(Number(rawLimit)) || Number(rawLimit) < 1)
      return error("limit must be a positive integer");
    const limit = Math.min(Number(rawLimit), EXPORT_PAGE_LIMIT);
    const page = store === "room" ? await room.exportPage(table, after, limit) : await hub.exportPage(table, after, limit);
    if ("error" in page) return error(page.error, page.status);
    return json({ store, table, ...page });
  } catch (err) {
    if (err instanceof HttpError) return error(err.message, err.status);
    return error(recovery ? "recovery failed" : "export failed", 500);
  }
}

// Legacy data belongs only to the seeded default team; hub transcript entries
// belong to every member's own team. Never bind legacy data by editable names.
async function legacyAdapter(request: Request, env: Env, ip: string): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.slice("/api/legacy".length);
  if (request.method !== "GET") return error("method not allowed", 405);
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
    // Read immutable seed metadata; authenticating as seeded agents would
    // falsely refresh their presence when a generated member opens this panel.
    defaultTeam = (await hub.legacyTeamId()) ?? undefined;
    if (defaultTeam) legacyTeams.set(env, defaultTeam);
  }
  if (path === "/transcript") {
    const rawAfter = url.searchParams.get("after");
    const after = Number(rawAfter ?? 0);
    if ((rawAfter !== null && !/^-?\d+$/.test(rawAfter)) || !Number.isSafeInteger(after))
      return error("after must be an integer");
    const hubMessages = await hub.teamTranscript(team);
    let snapshot: any = { messages: [], tasks: [], patches: [], audit: [], events: [], last_event_id: 0 };
    if (defaultTeam && team === defaultTeam) {
      const legacyUrl = new URL("/internal/transcript", url.origin);
      if (url.searchParams.has("after")) legacyUrl.searchParams.set("after", url.searchParams.get("after")!);
      const legacyResponse = await env.ROOM.get(env.ROOM.idFromName("default-room")).fetch(new Request(legacyUrl, {
        headers: { "x-agentmesh-agent": "legacy-adapter" },
      }));
      if (!legacyResponse.ok) return legacyResponse;
      snapshot = await legacyResponse.json();
    }
    // Both sources retain their own chronological tie ordering; legacy rows
    // precede hub rows at an identical timestamp. Keep only the newest 500.
    snapshot.messages = [
      ...snapshot.messages.map((message: any) => ({ ...message, kind: "direct" })),
      ...hubMessages,
    ].sort((a, b) => a.created_at - b.created_at).slice(-500);
    return json(snapshot);
  }
  if (!defaultTeam || team !== defaultTeam) return error("legacy panels are available only for the default team", 403);
  const legacyUrl = new URL("/v1" + path, url.origin);
  if (url.searchParams.has("after")) legacyUrl.searchParams.set("after", url.searchParams.get("after")!);
  // Only this fixed GET allowlist can enter the legacy room; caller-supplied
  // identity headers and hub credentials are never forwarded to it.
  return env.ROOM.get(env.ROOM.idFromName("default-room")).fetch(new Request(legacyUrl, {
    headers: { "x-agentmesh-agent": "agent-a" },
  }));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/admin/export" || url.pathname.startsWith("/admin/export/") ||
        url.pathname === "/admin/restore" || url.pathname === "/admin/import") return adminExport(request, env);
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
      if (url.pathname !== "/") return new Response(null, { status: 302, headers: { location: "/", "cache-control": "no-store" } });
      const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
      return new Response(request.method === "HEAD" ? null : dashboard.replaceAll("__AGENTMESH_NONCE__", nonce), { headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "content-security-policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'`,
      }});
    }

    const header = request.headers.get("authorization") ?? "";
    const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
    const agent = token ? await identify(token, env) : null;
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
  private ownerRecovery: AdminRecovery;
  private waiters = new Set<() => void>();
  private coordination!: Coordination;
  private deferredPushes: Array<() => void> | null = null;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.ownerRecovery = new AdminRecovery(ctx);
  }

  // Export RPCs must not create/migrate data or schedule alarms on cold starts.
  // Normal application traffic retains the existing initialization lifecycle.
  private initialize() {
    if (this.coordination) return;
    const ctx = this.ctx;
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

  prepareRestore(time: number, dryRun: boolean) { return this.ownerRecovery.prepareRestore(time, dryRun); }
  abortRestore() { this.ownerRecovery.abortRestore(); }
  importRows(table: string, rows: unknown) { return this.ownerRecovery.importRows(table, rows); }
  exportTables() { this.ownerRecovery.assertReady(); return exportTables(this.ctx.storage.sql); }
  exportPage(table: string, after: string | null, limit: number) {
    this.ownerRecovery.assertReady();
    return exportPage(this.ctx.storage.sql, table, after, limit);
  }

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
    const agent = request.headers.get("x-agentmesh-agent") as Agent | "legacy-adapter";

    try {
      this.ownerRecovery.assertReady();
      this.initialize();
      // Only the team-authenticated adapter can request a full legacy snapshot.
      const legacySnapshot = agent === "legacy-adapter" && path === "/internal/transcript" && method === "GET";
      const dashboardState = path === "/v1/dashboard/state" && method === "GET";
      if (legacySnapshot || dashboardState) {
        if (!legacySnapshot && !AGENTS.includes(agent as Agent)) return error("invalid agent context", 401);
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
        const snapshot = this.coordination.snapshot(Date.now(), legacySnapshot ? undefined : agent as Agent);
        return json({
          messages: (dashboardState
            ? this.sql("SELECT * FROM messages WHERE sender=? OR recipient=? ORDER BY created_at DESC, rowid DESC LIMIT 500", agent, agent)
            : this.sql("SELECT * FROM messages ORDER BY created_at DESC, rowid DESC LIMIT 500")).toArray().reverse(),
          tasks,
          patches: this.sql("SELECT id, sender, title, task_id, created_at, length(text) AS size FROM patches ORDER BY created_at DESC LIMIT 50").toArray().reverse(),
          audit: (dashboardState
            ? this.sql("SELECT * FROM audit WHERE actor=? ORDER BY id DESC LIMIT 200", agent)
            : this.sql("SELECT * FROM audit ORDER BY id DESC LIMIT 200")).toArray().reverse(),
          events,
          last_event_id: events.length ? events[events.length - 1].id : after,
          ...snapshot,
          ...(dashboardState ? { me: agent } : {}),
          ...(dashboardState && this.env.HUB ? { team_transcript: true } : {}),
        });
      }
      if (agent === "legacy-adapter" || !AGENTS.includes(agent)) return error("invalid agent context", 401);
      if (path === "/v1/transcript") return error("team member key required", 403);
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

  // Only live file claims expire; conversation and coordination history is permanent.
  async alarm() {
    this.ownerRecovery.assertReady();
    this.initialize();
    const now = Date.now();
    this.sql("DELETE FROM file_claims WHERE expires_at<=?", now);
    await this.ctx.storage.setAlarm(now + DAY);
  }
}
