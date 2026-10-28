// Provenance: OpenHuddle 59105e5 src/hub/server.js, MIT; adapted: Worker Request/Response routing, SSE, Durable Object lifecycle, read-only owner export and owner recovery/import RPCs
import { DurableObject } from "cloudflare:workers";
import { DoSqlite } from "./do-sqlite";
import { openDb } from "./src/hub/db-do.js";
import { Hub } from "./src/hub/hub.js";
import { HubError } from "./src/hub/errors.js";
import { sha256 } from "./src/shared/ids.js";
import { LIMITS, ASK_WAIT_MAX_S } from "./src/shared/protocol.js";
import { seedHub } from "./agentmesh-seed.js";
import { exportTables, exportPage } from "./export";
import { AdminRecovery } from "./admin-recovery";
import { HttpError } from "../coordination";
import { teamTranscript } from "./transcript";

interface HubEnv { AGENT_A_TOKEN: string; AGENT_B_TOKEN: string; }
const PING_MS = 25_000;
const SWEEP_MS = 600_000;
const LIMITER_KEYS = 50_000;

// Same token bucket and budgets as upstream server.js.
class Limiter {
  private buckets = new Map<string, { tokens: number; at: number }>();
  constructor(private capacity: number, private windowMs: number) {}
  take(key: string) {
    const now = Date.now();
    if (this.buckets.size >= LIMITER_KEYS) {
      this.sweep();
      if (this.buckets.size >= LIMITER_KEYS) this.buckets.clear();
    }
    const b = this.buckets.get(key) || { tokens: this.capacity, at: now };
    b.tokens = Math.min(this.capacity, b.tokens + ((now - b.at) * this.capacity) / this.windowMs);
    b.at = now;
    const ok = b.tokens >= 1;
    if (ok) b.tokens -= 1;
    this.buckets.set(key, b);
    return ok;
  }
  sweep() {
    const now = Date.now();
    for (const [key, b] of this.buckets) if (now - b.at > this.windowMs) this.buckets.delete(key);
  }
}

function sendJson(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: {
    "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  } });
}

async function readBody(request: Request): Promise<any> {
  if (Number(request.headers.get("content-length") || 0) > LIMITS.body) {
    await request.body?.cancel();
    throw new HubError(413, "too_large", "Request body is too large.");
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
        if (size > LIMITS.body) {
          await reader.cancel();
          throw new HubError(413, "too_large", "Request body is too large.");
        }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  const text = new TextDecoder().decode(bytes).trim();
  if (!text) return {};
  try {
    const body = JSON.parse(text);
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("not an object");
    return body;
  } catch { throw new HubError(400, "bad_json", "Body must be a JSON object."); }
}

export class HubRoom extends DurableObject<HubEnv> {
  private ownerRecovery: AdminRecovery;
  private hub: any;
  private seeded = false;
  private openLimiter = new Limiter(20, 3600_000);
  private memberLimiter = new Limiter(600, 60_000);
  private authLimiter = new Limiter(60, 60_000);
  private streams = new Set<() => void>();
  private memberStreams = new Map<string, Array<() => void>>();

  constructor(ctx: DurableObjectState, env: HubEnv) {
    super(ctx, env);
    this.ownerRecovery = new AdminRecovery(ctx);
  }

  private initialize() {
    if (this.hub) return;
    const ctx = this.ctx;
    this.hub = new Hub(openDb(new DoSqlite(ctx.storage)));
    ctx.blockConcurrencyWhile(async () => {
      if ((await ctx.storage.getAlarm()) === null) await ctx.storage.setAlarm(Date.now() + SWEEP_MS);
    });
  }

  exportTokenIsMember(hash: string): boolean {
    if (!this.ctx.storage.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='members'").toArray().length) return false;
    return this.ctx.storage.sql.exec("SELECT 1 FROM members WHERE token_hash=? LIMIT 1", hash).toArray().length > 0;
  }

  prepareRestore(time: number, dryRun: boolean) { return this.ownerRecovery.prepareRestore(time, dryRun); }
  abortRestore() { this.ownerRecovery.abortRestore(); }
  importRows(table: string, rows: unknown) { return this.ownerRecovery.importRows(table, rows); }
  exportTables() { this.ownerRecovery.assertReady(); return exportTables(this.ctx.storage.sql); }
  exportPage(table: string, after: string | null, limit: number) {
    this.ownerRecovery.assertReady();
    return exportPage(this.ctx.storage.sql, table, after, limit);
  }

  // Internal RPC only. The Worker authenticates membership and checks the
  // requested team with /api/state before requesting this read-only projection.
  legacyTeamId(): string | null {
    this.ownerRecovery.assertReady();
    const rows = this.ctx.storage.sql.exec<{ team_id: string }>("SELECT team_id FROM agentmesh_seed LIMIT 1").toArray();
    return rows[0]?.team_id ?? null;
  }

  teamTranscript(teamId: string) {
    this.ownerRecovery.assertReady();
    return teamTranscript(this.ctx.storage.sql, teamId);
  }

  async alarm() {
    this.ownerRecovery.assertReady();
    this.initialize();
    this.openLimiter.sweep();
    this.memberLimiter.sweep();
    this.authLimiter.sweep();
    this.hub.sweep();
    await this.ctx.storage.setAlarm(Date.now() + SWEEP_MS);
  }

  private stream(request: Request, member: any, since: number) {
    if (this.streams.size >= LIMITS.streams_total) throw new HubError(503, "busy", "The hub is full right now. Try again in a minute.");
    let mine = this.memberStreams.get(member.id);
    if (!mine) this.memberStreams.set(member.id, (mine = []));
    while (mine.length >= LIMITS.streams_per_member) mine[0]();
    let cursor = Math.min(since, this.hub.lastEventId(member.team_id));
    let closed = false;
    let unsubscribe: (() => void) | undefined;
    let ping: ReturnType<typeof setInterval> | undefined;
    let controller: ReadableStreamDefaultController<Uint8Array>;
    const encoder = new TextEncoder();
    const finish = () => {
      if (closed) return;
      closed = true;
      if (ping !== undefined) clearInterval(ping);
      unsubscribe?.();
      request.signal.removeEventListener("abort", finish);
      this.hub.streamClosed(member.id);
      this.streams.delete(finish);
      const index = mine!.indexOf(finish);
      if (index !== -1) mine!.splice(index, 1);
      if (!mine!.length) this.memberStreams.delete(member.id);
      try { controller.close(); } catch { /* consumer canceled */ }
    };
    const write = (text: string) => {
      if (closed) return;
      controller.enqueue(encoder.encode(text));
      // Bound bytes, not the number of queued events, just like writableLength upstream.
      if ((controller.desiredSize ?? 0) < 0) finish();
    };
    const writeEvent = (ev: any) => {
      if (closed) return;
      cursor = ev.id;
      write(`id: ${ev.id}\nevent: ${ev.kind}\ndata: ${JSON.stringify(ev)}\n\n`);
    };
    const body = new ReadableStream<Uint8Array>({
      start: c => {
        controller = c;
        mine!.push(finish);
        this.hub.streamOpened(member.id);
        this.streams.add(finish);
        request.signal.addEventListener("abort", finish, { once: true });
        write(": connected\n\n");
        for (const ev of this.hub.events(member, { since: cursor, limit: 500 }).events) writeEvent(ev);
        if (closed) return;
        unsubscribe = this.hub.subscribe(member.team_id, (ev: any) => {
          if (ev.id <= cursor) return;
          writeEvent(ev);
          if (ev.kind === "member_removed" && ev.data?.member_id === member.id) finish();
        });
        ping = setInterval(() => write(": ping\n\n"), PING_MS);
        // Node test harness only; Workers timers do not have unref.
        (ping as any).unref?.();
        if (request.signal.aborted) finish();
      },
      cancel: finish,
    }, { highWaterMark: LIMITS.stream_buffer, size: chunk => chunk.byteLength });
    return new Response(body, { headers: {
      "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache, no-transform",
      "x-accel-buffering": "no",
    } });
  }

  async fetch(request: Request): Promise<Response> {
    try {
      this.ownerRecovery.assertReady();
      this.initialize();
      const url = new URL(request.url);
      this.hub.publicUrl = url.origin;
      if (!this.seeded) {
        seedHub(this.hub, {
          "agent-a": sha256((this.env.AGENT_A_TOKEN || "").split(",")[0].trim()),
          "agent-b": sha256((this.env.AGENT_B_TOKEN || "").split(",")[0].trim()),
        });
        this.seeded = true;
      }
      const path = url.pathname;
      const method = request.method;
      // The outer Worker overwrites this header with Cloudflare's trusted client address.
      const ip = request.headers.get("x-agentmesh-client-ip") || "unknown";
      if (method === "POST" && (path === "/api/teams" || path === "/api/join")) {
        if (!this.openLimiter.take(ip)) throw new HubError(429, "rate_limited", "Too many new teams or joins from here. Try again later.");
        const body = await readBody(request);
        return sendJson(200, path === "/api/teams" ? this.hub.createTeam(body) : this.hub.join(body));
      }
      const auth = request.headers.get("authorization") || "";
      const token = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
      let member;
      try { member = this.hub.auth(token); }
      catch (err) {
        if (err instanceof HubError && err.status === 401 && !this.authLimiter.take(ip)) {
          throw new HubError(429, "rate_limited", "Too many requests without a valid token from here. Try again in a minute.");
        }
        throw err;
      }
      if (!this.memberLimiter.take(member.id)) throw new HubError(429, "rate_limited", "Slow down: too many requests.");
      if (method === "GET") {
        if (path === "/api/state") return sendJson(200, this.hub.state(member));
        if (path === "/api/events") {
          const q = url.searchParams;
          return sendJson(200, this.hub.events(member, { since: q.get("since"), limit: q.get("limit"), urgent: q.get("urgent") === "1", tail: q.get("tail") }));
        }
        if (path === "/api/huddle") return sendJson(200, this.hub.huddle(member, { id: url.searchParams.get("id") }));
        if (path === "/api/stream") return this.stream(request, member, Number(request.headers.get("last-event-id") || url.searchParams.get("since") || 0) || 0);
        throw new HubError(404, "not_found", `No route GET ${path}.`);
      }
      if (method !== "POST") throw new HubError(405, "method_not_allowed", "Use GET or POST.");
      const body = await readBody(request);
      if (path === "/api/ask") {
        const result = this.hub.ask(member, body);
        let answer = null;
        if (result.to.online) {
          const wait = Math.min(Math.max(Number(body.wait_s ?? ASK_WAIT_MAX_S) || 0, 0), ASK_WAIT_MAX_S);
          if (wait > 0) answer = await this.hub.waitAnswer(member.team_id, result.ask_id, wait * 1000);
        }
        return sendJson(200, { ...result, answer });
      }
      const calls: Record<string, () => any> = {
        "/api/heartbeat": () => this.hub.heartbeat(member, body.status),
        "/api/post": () => this.hub.post(member, body),
        "/api/reply": () => this.hub.reply(member, body),
        "/api/huddle/start": () => this.hub.huddleStart(member, body),
        "/api/huddle/post": () => this.hub.huddlePost(member, body),
        "/api/huddle/vote": () => this.hub.huddleVote(member, body),
        "/api/huddle/close": () => this.hub.huddleClose(member, body),
        "/api/prompt/edit": () => this.hub.promptEdit(member, body),
        "/api/prompt/submit": () => this.hub.promptSubmit(member),
        "/api/prompt/vote": () => this.hub.promptVote(member, body),
        "/api/plan": () => this.hub.plan(member, body),
        "/api/task": () => this.hub.taskAction(member, body),
        "/api/claims": () => this.hub.claims(member, body),
        "/api/file-edit": () => this.hub.fileEdit(member, body),
        "/api/commit": () => this.hub.commit(member, body),
        "/api/settings": () => this.hub.settings(member, body),
        "/api/invite": () => this.hub.invite(member, body),
        "/api/members/remove": () => this.hub.removeMember(member, body),
        "/api/leave": () => this.hub.leave(member),
        "/api/team/delete": () => this.hub.deleteOwnTeam(member),
      };
      if (!calls[path]) throw new HubError(404, "not_found", `No route POST ${path}.`);
      return sendJson(200, calls[path]());
    } catch (err) {
      if (err instanceof HttpError) return sendJson(err.status, { error: err.message, code: "recovery_pending" });
      if (err instanceof HubError) return sendJson(err.status, { error: err.message, code: err.code });
      return sendJson(500, { error: "Something went wrong on the hub.", code: "internal" });
    }
  }
}
