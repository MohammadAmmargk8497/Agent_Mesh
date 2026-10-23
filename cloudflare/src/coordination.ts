// Additive coordination state; all data remains in the Room's existing SQLite store.
type Agent = "agent-a" | "agent-b";
const AGENTS: Agent[] = ["agent-a", "agent-b"];
const DAY = 86_400_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type Sql = (query: string, ...args: any[]) => any;
type Emit = (recipient: string, actor: Agent, type: string, ref: string, includeSelf?: boolean) => void;
export class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export function cleanText(value: unknown, max = 8000): string {
  if (typeof value !== "string") throw new HttpError(400, "text must be a string");
  const text = value.replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "").trim();
  if (/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(text)) throw new HttpError(400, "text must contain valid Unicode");
  if (!text || text.length > max) throw new HttpError(400, `text must contain 1-${max} characters`);
  return text;
}
function filePath(value: unknown): string {
  if (typeof value !== "string" || /[\x00-\x1f\x7f-\x9f]/.test(value)) throw new HttpError(400, "invalid path");
  const path = cleanText(value, 500).replaceAll("\\", "/");
  if (path.startsWith("/") || /^[a-z]:/i.test(path) || path.split("/").includes("..")) throw new HttpError(400, "path must be repository-relative");
  const normalized = path.split("/").filter(p => p && p !== ".").join("/");
  if (!normalized) throw new HttpError(400, "invalid path");
  return normalized;
}
function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff" } });
}
function optionalId(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value !== "string" || !UUID.test(value)) throw new HttpError(400, "task_id must be a UUID");
  return value;
}
async function readJson(request: Request): Promise<any> {
  const maximum = 256 * 1024;
  if (Number(request.headers.get("content-length") ?? "0") > maximum) throw new HttpError(413, "request body too large");
  const reader = request.body?.getReader(), chunks: Uint8Array[] = [];
  let length = 0;
  if (reader) {
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > maximum) {
          await reader.cancel();
          throw new HttpError(413, "request body too large");
        }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
  }
  const buf = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { buf.set(chunk, offset); offset += chunk.byteLength; }
  let body: unknown;
  try { body = JSON.parse(new TextDecoder().decode(buf)); } catch { throw new HttpError(400, "invalid JSON"); }
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new HttpError(400, "body must be a JSON object");
  return body;
}

export class Coordination {
  constructor(private sql: Sql, private emit: Emit, private transact: <T>(callback: () => T) => T) {
    const columns = new Set(this.sql("PRAGMA table_info(tasks)").toArray().map((r: any) => r.name));
    for (const [name, type] of [["owner", "TEXT"], ["file_areas", "TEXT"], ["depends_on", "TEXT"], ["contract", "INTEGER DEFAULT 0"]]) {
      if (!columns.has(name)) this.sql(`ALTER TABLE tasks ADD COLUMN ${name} ${type}`);
    }
    this.sql(`
      CREATE TABLE IF NOT EXISTS master_sections (section TEXT PRIMARY KEY, text TEXT NOT NULL, author TEXT NOT NULL, revision INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS master_edits (id TEXT PRIMARY KEY, section TEXT NOT NULL, text TEXT NOT NULL, author TEXT NOT NULL, revision INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS master_state (id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL, revision INTEGER NOT NULL, locked_version INTEGER NOT NULL, round INTEGER NOT NULL, lead TEXT);
      INSERT OR IGNORE INTO master_state (id,status,revision,locked_version,round,lead) VALUES (1,'draft',0,0,0,NULL);
      CREATE TABLE IF NOT EXISTS master_votes (round INTEGER NOT NULL, agent TEXT NOT NULL, decision TEXT NOT NULL, note TEXT, PRIMARY KEY(round,agent));
      CREATE TABLE IF NOT EXISTS file_claims (path TEXT PRIMARY KEY, agent TEXT NOT NULL, task_id TEXT, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS huddles (id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, topic TEXT NOT NULL, status TEXT NOT NULL, decision TEXT, created_at INTEGER NOT NULL, closed_at INTEGER);
      CREATE TABLE IF NOT EXISTS huddle_posts (id TEXT PRIMARY KEY, huddle_id TEXT NOT NULL, author TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, ref_post_id TEXT, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS huddle_votes (huddle_id TEXT NOT NULL, ref_post_id TEXT NOT NULL, agent TEXT NOT NULL, vote TEXT NOT NULL, note TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(huddle_id,ref_post_id,agent));
      CREATE TABLE IF NOT EXISTS dashboard_tasks (task_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('open','assigned')));
      CREATE TRIGGER IF NOT EXISTS dashboard_tasks_status AFTER UPDATE OF status ON tasks WHEN NEW.status<>'open' OR OLD.status<>'open' BEGIN DELETE FROM dashboard_tasks WHERE task_id=NEW.id; END;
      CREATE TABLE IF NOT EXISTS questions (id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL, task_id TEXT, answer TEXT, created_at INTEGER NOT NULL, answered_at INTEGER);
      CREATE TABLE IF NOT EXISTS news (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, summary TEXT NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS news_seen (agent TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS news_details (news_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS member_settings (agent TEXT PRIMARY KEY, auto_respond INTEGER NOT NULL DEFAULT 1 CHECK(auto_respond IN (0,1)));
      CREATE INDEX IF NOT EXISTS huddle_posts_huddle ON huddle_posts(huddle_id,created_at);
    `);
  }
  private rows(query: string, ...args: any[]): any[] { return this.sql(query, ...args).toArray(); }
  private one(query: string, ...args: any[]): any { return this.rows(query, ...args)[0]; }
  private autoRespondEnabled(agent: Agent): boolean {
    const settings = this.one("SELECT auto_respond FROM member_settings WHERE agent=?", agent);
    return settings ? Boolean(settings.auto_respond) : true;
  }
  private automaticReply(body: any, agent: Agent): boolean {
    if (body.auto_respond !== undefined && typeof body.auto_respond !== "boolean") throw new HttpError(400, "auto_respond must be a boolean");
    const automatic = body.auto_respond === true;
    if (automatic && !this.autoRespondEnabled(agent)) throw new HttpError(409, "auto_respond is disabled for this member");
    return automatic;
  }
  private state(): any {
    return this.one("SELECT status,revision,locked_version,round,lead FROM master_state WHERE id=1");
  }
  private news(actor: Agent, kind: string, ref: string, summary: string, now: number, details?: unknown): number {
    let capped = summary.slice(0, 8000);
    if (/[\uD800-\uDBFF]$/.test(capped)) capped = capped.slice(0, -1);
    const id = Number(this.sql("INSERT INTO news(actor,kind,ref,summary,created_at) VALUES (?,?,?,?,?) RETURNING id", actor, kind, ref, capped, now).one().id);
    if (details !== undefined) this.sql("INSERT INTO news_details(news_id,data) VALUES (?,?)", id, JSON.stringify(details));
    return id;
  }
  taskNews(actor: Agent, kind: string, ref: string, now: number) {
    this.news(actor, kind, ref, `Task ${kind.slice(5).replaceAll("_", " ")}`, now);
  }
  private broadcast(actor: Agent, kind: string, ref: string, includeSelf = false) {
    for (const other of AGENTS) this.emit(other, actor, kind, ref, includeSelf);
  }
  master(): any {
    const state = this.state();
    const votes = this.rows("SELECT round,agent,decision,note FROM master_votes WHERE round=? ORDER BY agent", state.round);
    return { state, sections: this.rows("SELECT * FROM master_sections ORDER BY section"), votes,
      waiting_on: state.status === "voting" ? AGENTS.filter(a => !votes.some(v => v.agent === a && v.decision === "approve")) : [],
      edits: this.rows("SELECT * FROM master_edits ORDER BY revision DESC LIMIT 50").reverse() };
  }
  private dependencyTasks(dependencies: string[]): Map<string, any> {
    const pending = [...dependencies], found = new Map<string, any>();
    for (const id of pending) {
      if (found.has(id)) continue;
      const task = this.one("SELECT status,depends_on FROM tasks WHERE id=?", id);
      found.set(id, task);
      if (task) pending.push(...JSON.parse(task.depends_on || "[]"));
    }
    return found;
  }
  parseTask(row: any): any {
    const depends_on: string[] = JSON.parse(row.depends_on || "[]");
    return { ...row, acceptance_criteria: JSON.parse(row.acceptance_criteria), owner: row.owner ?? null,
      file_areas: JSON.parse(row.file_areas || "[]"), depends_on, contract: Boolean(row.contract),
      waiting_on: [...this.dependencyTasks(depends_on)].filter(([, task]) => !["done", "accepted"].includes(task?.status)).map(([id]) => id) };
  }
  private visibleTask(id: string, agent: Agent): any {
    const task = this.one("SELECT * FROM tasks WHERE id=? AND (assignee=? OR created_by=?)", id, agent, agent);
    if (!task) throw new HttpError(400, "task not found or not visible");
    return task;
  }
  taskExtras(body: any, agent: Agent): any {
    const owner = body.owner === undefined ? body.assignee : body.owner;
    if (!AGENTS.includes(owner)) throw new HttpError(400, "owner must be agent-a or agent-b");
    const areas = body.file_areas === undefined ? [] : body.file_areas, depends = body.depends_on === undefined ? [] : body.depends_on;
    if (!Array.isArray(areas) || areas.length > 20) throw new HttpError(400, "file_areas must contain at most 20 paths");
    const file_areas = areas.map(v => filePath(cleanText(v, 500)));
    if (!Array.isArray(depends) || depends.length > 20 || depends.some(v => typeof v !== "string" || !UUID.test(v))) throw new HttpError(400, "depends_on must contain at most 20 UUIDs");
    for (const id of depends) this.visibleTask(id, agent);
    if (body.contract !== undefined && typeof body.contract !== "boolean") throw new HttpError(400, "contract must be a boolean");
    if (body.skeleton !== undefined && typeof body.skeleton !== "boolean") throw new HttpError(400, "skeleton must be a boolean");
    return { owner, file_areas, depends_on: [...new Set(depends)], contract: Boolean(body.contract || body.skeleton) };
  }
  private taskInput(body: any, agent: Agent): any {
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new HttpError(400, "task must be an object");
    const title = cleanText(body.title, 200), description = cleanText(body.description);
    if (!AGENTS.includes(body.assignee)) throw new HttpError(400, "assignee must be agent-a or agent-b");
    const criteria = body.acceptance_criteria === undefined ? [] : body.acceptance_criteria;
    if (!Array.isArray(criteria) || criteria.length > 20) throw new HttpError(400, "acceptance_criteria must contain at most 20 strings");
    return { ...body, title, description, acceptance_criteria: criteria.map(v => cleanText(v, 500)), ...this.taskExtras({ ...body, owner: body.assignee }, agent) };
  }
  private createTask(body: any, agent: Agent, now: number): any {
    const id = crypto.randomUUID();
    this.sql("INSERT INTO tasks(id,title,description,assignee,created_by,status,acceptance_criteria,result,created_at,updated_at,owner,file_areas,depends_on,contract) VALUES (?,?,?,?,?,'open',?,NULL,?,?,?,?,?,?)",
      id, body.title, body.description, body.assignee, agent, JSON.stringify(body.acceptance_criteria), now, now, body.owner,
      JSON.stringify(body.file_areas), JSON.stringify(body.depends_on), Number(body.contract));
    this.emit(body.assignee, agent, "task_assigned", id);
    this.taskNews(agent, "task_created", id, now);
    return this.parseTask(this.one("SELECT * FROM tasks WHERE id=?", id));
  }
  taskUnblocked(id: string, actor: Agent, now: number) {
    for (const raw of this.rows("SELECT * FROM tasks WHERE status IN ('open','in_progress','blocked')")) {
      const task = this.parseTask(raw);
      if (this.dependencyTasks(task.depends_on).has(id) && !task.waiting_on.length) {
        this.emit(task.owner || task.assignee, actor, "task_unblocked", task.id, true);
        this.news(actor, "task_unblocked", task.id, "Task dependencies satisfied", now);
      }
    }
  }
  huddle(id: string, includeDashboard = false): any {
    const huddle = this.one("SELECT * FROM huddles WHERE id=?", id);
    if (!huddle) throw new HttpError(404, "huddle not found");
    const posts = this.rows("SELECT * FROM huddle_posts WHERE huddle_id=? ORDER BY created_at,rowid", id);
    const votes = includeDashboard ? this.rows("SELECT * FROM huddle_votes WHERE huddle_id=? ORDER BY created_at,agent", id) : [];
    const waiting = huddle.status === "open" ? AGENTS.filter(a => !posts.some(p => p.author === a)) : [];
    return { ...huddle, posts, waiting_on: waiting, ...(includeDashboard ? { votes,
      dashboard_waiting_on: waiting.filter(a => !(huddle.kind !== "standup" && huddle.owner === a) && !votes.some(v => v.agent === a)) } : {}) };
  }
  snapshot(now: number, agent?: Agent): any {
    const visible = agent ? new Set(this.rows("SELECT id FROM tasks WHERE created_by=? OR assignee=? OR (status='open' AND id IN (SELECT task_id FROM dashboard_tasks WHERE state='open'))", agent, agent).map(row => row.id)) : undefined;
    const news = this.rows("SELECT * FROM news ORDER BY id DESC LIMIT 200").reverse()
      .filter(item => !visible || !item.kind.startsWith("task_") || visible.has(item.ref));
    return { master_prompt: this.master(),
      huddles: this.rows("SELECT id FROM huddles ORDER BY created_at DESC LIMIT 100").map(h => {
        const item = this.huddle(h.id, true);
        if (agent) item.waiting_on = item.dashboard_waiting_on;
        return item;
      }),
      claims: this.rows("SELECT * FROM file_claims WHERE expires_at>? ORDER BY path", now),
      questions: (agent ? this.rows("SELECT * FROM questions WHERE sender=? OR recipient=? ORDER BY created_at DESC LIMIT 200", agent, agent)
        : this.rows("SELECT * FROM questions ORDER BY created_at DESC LIMIT 200")).reverse(),
      news,
      feed_details: Object.fromEntries(this.rows("SELECT * FROM news_details WHERE news_id IN (SELECT id FROM news ORDER BY id DESC LIMIT 200)").map(row => [String(row.news_id), JSON.parse(row.data)])),
      member_settings: AGENTS.map(agent => ({ agent, auto_respond: this.autoRespondEnabled(agent) })) };
  }
  dashboardStatus(task: any): string {
    if (task.status !== "open") return task.status;
    return this.one("SELECT state FROM dashboard_tasks WHERE task_id=?", task.id)?.state ?? "proposed";
  }
  private dashboardTaskAction(id: string, action: string, body: any, agent: Agent, now: number): Response {
    const shared = action === "take" ? " OR (status='open' AND EXISTS (SELECT 1 FROM dashboard_tasks WHERE task_id=tasks.id AND state='open'))" : "";
    const raw = this.one(`SELECT * FROM tasks WHERE id=? AND (created_by=? OR assignee=?${shared})`, id, agent, agent);
    if (!raw) throw new HttpError(404, "task not found");
    const task = this.parseTask(raw), status = this.dashboardStatus(task);
    const summary = body.summary === undefined ? "Completed from dashboard" : cleanText(body.summary);
    let nextStatus: string;
    if (["accept", "decline"].includes(action)) {
      if (task.assignee !== agent) throw new HttpError(403, "only the proposed assignee may respond");
      if (status !== "proposed") throw new HttpError(409, "task is not proposed");
      nextStatus = action === "accept" ? "assigned" : "open";
    } else if (action === "take") {
      if (!["proposed", "open"].includes(status)) throw new HttpError(409, "task is not available");
      nextStatus = "assigned";
      this.sql("UPDATE tasks SET assignee=? WHERE id=?", agent, id);
    } else {
      if (task.assignee !== agent) throw new HttpError(403, "only the assignee may update this task");
      const allowed = action === "release" ? ["assigned", "in_progress"] : action === "start" ? ["assigned"] : ["in_progress"];
      if (!allowed.includes(status)) throw new HttpError(409, "task is not in the required state");
      if (action === "start" && task.waiting_on.length) return json({ error: "waiting on dependencies", waiting_on: task.waiting_on }, 409);
      nextStatus = ({ start: "in_progress", release: "open", finish: "done" } as Record<string, string>)[action];
    }
    const legacyStatus = ["open", "assigned"].includes(nextStatus) ? "open" : nextStatus;
    this.sql("UPDATE tasks SET status=?,updated_at=? WHERE id=?", legacyStatus, now, id);
    if (["open", "assigned"].includes(nextStatus)) this.sql("INSERT INTO dashboard_tasks(task_id,state) VALUES (?,?) ON CONFLICT(task_id) DO UPDATE SET state=excluded.state", id, nextStatus);
    else this.sql("DELETE FROM dashboard_tasks WHERE task_id=?", id);
    if (action === "finish") this.sql("UPDATE tasks SET result=? WHERE id=?", summary, id);
    if (["release", "finish"].includes(action)) this.sql("DELETE FROM file_claims WHERE task_id=? AND agent=?", id, agent);
    const event = ({ take: "task_taken", accept: "task_accepted", decline: "task_declined", start: "task_claimed", release: "task_released", finish: "task_done" } as Record<string, string>)[action];
    this.news(agent, event, id, `Task ${action}: ${task.title}`, now);
    this.broadcast(agent, event, id);
    if (action === "finish") this.taskUnblocked(id, agent, now);
    return json({ id, status: legacyStatus, dashboard_status: nextStatus });
  }
  sweep(now: number) {
    this.sql("DELETE FROM file_claims WHERE expires_at<=?", now);
    this.sql("DELETE FROM news WHERE created_at<?", now - 30 * DAY);
    this.sql("DELETE FROM news_details WHERE news_id NOT IN (SELECT id FROM news)");
    this.sql("DELETE FROM huddle_posts WHERE huddle_id IN (SELECT id FROM huddles WHERE status='closed' AND closed_at<?)", now - 90 * DAY);
    this.sql("DELETE FROM huddles WHERE status='closed' AND closed_at<?", now - 90 * DAY);
    this.sql("DELETE FROM huddle_votes WHERE huddle_id NOT IN (SELECT id FROM huddles)");
    this.sql("DELETE FROM dashboard_tasks WHERE task_id NOT IN (SELECT id FROM tasks)");
    this.sql("DELETE FROM questions WHERE answered_at IS NOT NULL AND answered_at<?", now - 30 * DAY);
  }
  async route(request: Request, agent: Agent, path: string, method: string, url: URL, now: number): Promise<Response | undefined> {
    const known = ["/v1/settings", "/v1/master", "/v1/master/sections", "/v1/master/submit", "/v1/master/vote", "/v1/tasks/propose", "/v1/claims", "/v1/claims/release", "/v1/huddles", "/v1/questions", "/v1/news", "/v1/commits"].includes(path)
      || /^\/v1\/huddles\/[0-9a-f-]{36}(?:\/(?:posts|comments|votes|close))?$/i.test(path)
      || /^\/v1\/dashboard\/tasks\/[0-9a-f-]{36}\/(?:take|accept|decline|start|release|finish)$/i.test(path)
      || /^\/v1\/questions\/[0-9a-f-]{36}\/answer$/i.test(path);
    if (!known) return undefined;
    // Finish all asynchronous input and hashing before the atomic SQLite mutation.
    const mutation = ["/v1/settings", "/v1/master/sections", "/v1/master/submit", "/v1/master/vote", "/v1/tasks/propose", "/v1/claims", "/v1/claims/release", "/v1/huddles", "/v1/questions", "/v1/commits"].includes(path)
      || /^\/v1\/huddles\/[0-9a-f-]{36}\/(?:posts|comments|votes|close)$/i.test(path)
      || /^\/v1\/dashboard\/tasks\/[0-9a-f-]{36}\/(?:take|accept|decline|start|release|finish)$/i.test(path)
      || /^\/v1\/questions\/[0-9a-f-]{36}\/answer$/i.test(path);
    const body = method === "POST" && mutation ? await readJson(request) : undefined;
    let digest: string | undefined;
    if (path === "/v1/master/sections" && method === "POST") {
      if (this.state().status === "voting") throw new HttpError(409, "master prompt is voting");
      const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(cleanText(body.text)));
      digest = Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
    }
    return this.transact(() => this.handle(body, agent, path, method, url, now, digest));
  }
  private handle(body: any, agent: Agent, path: string, method: string, url: URL, now: number, digest?: string): Response | undefined {
    const dashboardTask = path.match(/^\/v1\/dashboard\/tasks\/([0-9a-f-]{36})\/(take|accept|decline|start|release|finish)$/i);
    if (dashboardTask) {
      if (method !== "POST") throw new HttpError(405, "method not allowed");
      return this.dashboardTaskAction(dashboardTask[1], dashboardTask[2].toLowerCase(), body, agent, now);
    }
    if (path === "/v1/settings" && ["GET", "POST"].includes(method)) {
      if (method === "POST") {
        if (typeof body.auto_respond !== "boolean") throw new HttpError(400, "auto_respond must be a boolean");
        this.sql("INSERT INTO member_settings(agent,auto_respond) VALUES (?,?) ON CONFLICT(agent) DO UPDATE SET auto_respond=excluded.auto_respond", agent, Number(body.auto_respond));
      }
      return json({ agent, auto_respond: this.autoRespondEnabled(agent) });
    }
    if (path === "/v1/master" && method === "GET") return json(this.master());
    if (path === "/v1/master/sections" && method === "POST") {
      const state = this.state();
      if (state.status === "voting") throw new HttpError(409, "master prompt is voting");
      const section = cleanText(body.section, 80), text = cleanText(body.text);
      const revision = state.revision + 1, id = crypto.randomUUID();
      this.sql("INSERT INTO master_sections(section,text,author,revision,sha256,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET text=excluded.text,author=excluded.author,revision=excluded.revision,sha256=excluded.sha256,created_at=excluded.created_at", section, text, agent, revision, digest, now);
      this.sql("INSERT INTO master_edits(id,section,text,author,revision,sha256,created_at) VALUES (?,?,?,?,?,?,?)", id, section, text, agent, revision, digest, now);
      this.sql("UPDATE master_state SET status='draft',revision=?,lead=COALESCE(lead,?) WHERE id=1", revision, agent);
      this.news(agent, "master_edited", section, `Master prompt section edited: ${section}`, now);
      this.broadcast(agent, "master_edited", section);
      return json({ revision, sha256: digest, author: agent }, 201);
    }
    if (path === "/v1/master/submit" && method === "POST") {
      const state = this.state();
      if (state.status !== "draft" || !state.revision) throw new HttpError(409, "master prompt must be a non-empty draft");
      const round = state.round + 1;
      this.sql("UPDATE master_state SET status='voting',round=? WHERE id=1", round);
      this.sql("INSERT INTO master_votes(round,agent,decision,note) VALUES (?,?,'approve',NULL)", round, agent);
      this.news(agent, "master_submitted", String(round), "Master prompt submitted for vote", now);
      this.broadcast(agent, "master_submitted", String(round));
      return json(this.master());
    }
    if (path === "/v1/master/vote" && method === "POST") {
      const state = this.state();
      if (state.status !== "voting") throw new HttpError(409, "master prompt is not voting");
      if (!["approve", "reject"].includes(body.decision)) throw new HttpError(400, "decision must be approve or reject");
      const note = body.note === undefined ? null : cleanText(body.note);
      this.sql("INSERT INTO master_votes(round,agent,decision,note) VALUES (?,?,?,?) ON CONFLICT(round,agent) DO UPDATE SET decision=excluded.decision,note=excluded.note", state.round, agent, body.decision, note);
      this.news(agent, "master_voted", String(state.round), "Master prompt vote recorded", now,
        { decision: body.decision, note, revision: state.revision });
      this.broadcast(agent, "master_voted", String(state.round));
      if (body.decision === "reject") this.sql("UPDATE master_state SET status='draft' WHERE id=1");
      else if (Number(this.one("SELECT COUNT(*) AS count FROM master_votes WHERE round=? AND decision='approve'", state.round).count) === AGENTS.length) {
        this.sql("UPDATE master_state SET status='locked',locked_version=locked_version+1 WHERE id=1");
        this.news(agent, "master_locked", String(state.round), "Master prompt locked", now);
        this.broadcast(agent, "master_locked", String(state.round));
      }
      return json(this.master());
    }
    if (path === "/v1/tasks/propose" && method === "POST") {
      const state = this.state();
      if (state.lead !== agent) throw new HttpError(403, "only the lead may propose tasks");
      if (state.status !== "locked") throw new HttpError(409, "master prompt must be locked");
      if (!Array.isArray(body.tasks) || !body.tasks.length || body.tasks.length > 20) throw new HttpError(400, "tasks must contain 1-20 tasks");
      const inputs = body.tasks.map((t: any) => this.taskInput(t, agent));
      const root = Math.max(0, inputs.findIndex((t: any) => t.contract));
      const tasks: any[] = new Array(inputs.length);
      tasks[root] = this.createTask({ ...inputs[root], contract: true }, agent, now);
      for (let i = 0; i < inputs.length; i++) {
        if (i === root) continue;
        const depends_on = [...new Set([...inputs[i].depends_on, tasks[root].id])];
        if (depends_on.length > 20) throw new HttpError(400, "depends_on must contain at most 20 task ids including the contract");
        tasks[i] = this.createTask({ ...inputs[i], depends_on }, agent, now);
      }
      return json({ tasks }, 201);
    }
    if (path === "/v1/claims" && method === "GET") {
      this.sql("DELETE FROM file_claims WHERE expires_at<=?", now);
      const pathFilter = url.searchParams.has("path") ? filePath(url.searchParams.get("path")) : null;
      return json({ agent, claims: pathFilter ? this.rows("SELECT * FROM file_claims WHERE path=?", pathFilter) : this.rows("SELECT * FROM file_claims ORDER BY path") });
    }
    if (path === "/v1/claims" && method === "POST") {
      this.sql("DELETE FROM file_claims WHERE expires_at<=?", now);
      const pathName = filePath(body.path), task_id = optionalId(body.task_id);
      if (task_id) this.visibleTask(task_id, agent);
      const held = this.one("SELECT * FROM file_claims WHERE path=? AND expires_at>?", pathName, now);
      if (held && held.agent !== agent) return json({ error: "file is claimed by another member", held_by: held.agent, expires_at: held.expires_at }, 409);
      const expires_at = now + 30 * 60_000;
      this.sql("INSERT INTO file_claims(path,agent,task_id,expires_at,created_at) VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET agent=excluded.agent,task_id=excluded.task_id,expires_at=excluded.expires_at,created_at=excluded.created_at", pathName, agent, task_id, expires_at, now);
      this.news(agent, "file_claimed", pathName, `File claimed: ${pathName}`, now);
      this.broadcast(agent, "file_claimed", pathName);
      return json(this.one("SELECT * FROM file_claims WHERE path=?", pathName), 201);
    }
    if (path === "/v1/claims/release" && method === "POST") {
      this.sql("DELETE FROM file_claims WHERE expires_at<=?", now);
      const pathName = filePath(body.path);
      const held = this.one("SELECT * FROM file_claims WHERE path=? AND expires_at>?", pathName, now);
      if (held && held.agent !== agent) throw new HttpError(403, "only the holder may release a claim");
      const changed = this.sql("DELETE FROM file_claims WHERE path=? AND agent=?", pathName, agent).rowsWritten;
      if (changed) { this.news(agent, "file_released", pathName, `File claim released: ${pathName}`, now); this.broadcast(agent, "file_released", pathName); }
      return json({ ok: true, changed });
    }
    if (path === "/v1/huddles" && method === "GET") {
      const status = url.searchParams.get("status");
      if (status !== null && !["open", "closed"].includes(status)) throw new HttpError(400, "status must be open or closed");
      const rows = status ? this.rows("SELECT id FROM huddles WHERE status=? ORDER BY created_at DESC LIMIT 100", status) : this.rows("SELECT id FROM huddles ORDER BY created_at DESC LIMIT 100");
      return json({ huddles: rows.map(h => this.huddle(h.id)) });
    }
    if (path === "/v1/huddles" && method === "POST") {
      const topic = cleanText(body.topic, 200);
      if (!["brainstorm", "design", "review", "standup"].includes(body.kind)) throw new HttpError(400, "invalid huddle kind");
      const id = crypto.randomUUID();
      this.sql("INSERT INTO huddles(id,owner,kind,topic,status,decision,created_at,closed_at) VALUES (?,?,?,?,'open',NULL,?,NULL)", id, agent, body.kind, topic, now);
      this.news(agent, "huddle_started", id, `Huddle started: ${topic}`, now); this.broadcast(agent, "huddle_started", id);
      return json({ id, created_at: now }, 201);
    }
    const hm = path.match(/^\/v1\/huddles\/([0-9a-f-]{36})(?:\/(posts|comments|votes|close))?$/i);
    if (hm) {
      const huddle = this.huddle(hm[1]);
      if (method === "GET" && !hm[2]) return json({ huddle });
      if (method === "POST" && ["comments", "votes"].includes(hm[2])) {
        if (huddle.status !== "open") throw new HttpError(409, "huddle is closed");
        const ref = body.ref_post_id ?? null;
        const referenced = typeof ref === "string" && UUID.test(ref) ? huddle.posts.find((p: any) => p.id === ref) : undefined;
        if ((ref !== null && !referenced) || (hm[2] === "votes" && !referenced)) throw new HttpError(400, "ref_post_id must refer to this huddle");
        if (hm[2] === "comments") {
          const text = cleanText(body.text), id = crypto.randomUUID();
          this.sql("INSERT INTO huddle_posts(id,huddle_id,author,kind,text,ref_post_id,created_at) VALUES (?,?,?,'comment',?,?,?)", id, huddle.id, agent, text, ref, now);
          this.news(agent, "huddle_commented", huddle.id, `Huddle comment: ${huddle.topic} — ${text}`, now); this.broadcast(agent, "huddle_commented", huddle.id);
          return json({ id, created_at: now }, 201);
        }
        if (referenced.kind !== "idea") throw new HttpError(400, "only ideas may receive votes");
        if (referenced.author === agent) throw new HttpError(403, "cannot vote on your own idea");
        if (!["up", "down"].includes(body.vote)) throw new HttpError(400, "vote must be up or down");
        const note = body.note === undefined ? null : cleanText(body.note, 500);
        this.sql("INSERT INTO huddle_votes(huddle_id,ref_post_id,agent,vote,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(huddle_id,ref_post_id,agent) DO UPDATE SET vote=excluded.vote,note=excluded.note,updated_at=excluded.updated_at", huddle.id, ref, agent, body.vote, note, now, now);
        this.news(agent, "huddle_voted", huddle.id, `${body.vote === "up" ? "Backed" : "Pushed back on"} idea ${ref} in ${huddle.topic}${note ? ": " + note : ""}`, now); this.broadcast(agent, "huddle_voted", huddle.id);
        return json({ huddle_id: huddle.id, ref_post_id: ref, agent, vote: body.vote, note });
      }
      if (method === "POST" && hm[2] === "posts") {
        if (huddle.status !== "open") throw new HttpError(409, "huddle is closed");
        const automatic = this.automaticReply(body, agent);
        if (automatic && huddle.posts.some((p: any) => p.author === agent)) throw new HttpError(409, "member has already contributed to this huddle");
        const text = cleanText(body.text, automatic ? 500 : 8000);
        if (!["idea", "back", "push_back"].includes(body.kind)) throw new HttpError(400, "invalid post kind");
        const ref_post_id = optionalId(body.ref_post_id);
        if (ref_post_id && !huddle.posts.some((p: any) => p.id === ref_post_id)) throw new HttpError(400, "referenced post is not in this huddle");
        const id = crypto.randomUUID();
        this.sql("INSERT INTO huddle_posts(id,huddle_id,author,kind,text,ref_post_id,created_at) VALUES (?,?,?,?,?,?,?)", id, huddle.id, agent, body.kind, text, ref_post_id, now);
        this.news(agent, "huddle_posted", huddle.id, `Huddle contribution: ${huddle.topic}`, now); this.broadcast(agent, "huddle_posted", huddle.id);
        return json({ id, created_at: now }, 201);
      }
      if (method !== "POST" || !["posts", "comments", "votes", "close"].includes(hm[2])) throw new HttpError(405, "method not allowed");
      if (method === "POST" && hm[2] === "close") {
        if (huddle.owner !== agent) throw new HttpError(403, "only the owner may close");
        if (huddle.status !== "open") throw new HttpError(409, "huddle is closed");
        const decision = cleanText(body.decision), supplied = body.tasks === undefined ? [] : body.tasks;
        if (!Array.isArray(supplied) || supplied.length > 10) throw new HttpError(400, "tasks must contain at most 10 tasks");
        const inputs = supplied.map(t => ({ ...this.taskInput(t, agent), owner: agent }));
        const tasks = inputs.map(t => this.createTask(t, agent, now));
        this.sql("UPDATE huddles SET status='closed',decision=?,closed_at=? WHERE id=?", decision, now, huddle.id);
        this.news(agent, "huddle_closed", huddle.id, `Huddle closed: ${huddle.topic} — ${decision}`, now); this.broadcast(agent, "huddle_closed", huddle.id, true);
        return json({ id: huddle.id, status: "closed", tasks: tasks.map(t => t.id) });
      }
    }
    if (path === "/v1/questions" && method === "GET") {
      const open = url.searchParams.get("open") === "true";
      return json({ questions: this.rows(`SELECT * FROM questions WHERE (sender=? OR recipient=?) ${open ? "AND answered_at IS NULL" : ""} ORDER BY created_at DESC LIMIT 100`, agent, agent).reverse() });
    }
    if (path === "/v1/questions" && method === "POST") {
      const text = cleanText(body.text), task_id = optionalId(body.task_id);
      if (!AGENTS.includes(body.to) || body.to === agent) throw new HttpError(400, "to must be the other agent");
      if (task_id) this.visibleTask(task_id, agent);
      const id = crypto.randomUUID();
      this.sql("INSERT INTO questions(id,sender,recipient,text,task_id,answer,created_at,answered_at) VALUES (?,?,?,?,?,NULL,?,NULL)", id, agent, body.to, text, task_id, now);
      this.news(agent, "question_asked", id, "Question asked", now); this.emit(body.to, agent, "question_asked", id);
      return json({ id, created_at: now }, 201);
    }
    const qm = path.match(/^\/v1\/questions\/([0-9a-f-]{36})\/answer$/i);
    if (qm && method === "POST") {
      const question = this.one("SELECT * FROM questions WHERE id=?", qm[1]);
      if (!question) throw new HttpError(404, "question not found");
      if (question.recipient !== agent) throw new HttpError(403, "only the recipient may answer");
      if (question.answered_at !== null) throw new HttpError(409, "question already answered");
      const automatic = this.automaticReply(body, agent);
      const text = cleanText(body.text, automatic ? 500 : 8000);
      this.sql("UPDATE questions SET answer=?,answered_at=? WHERE id=?", text, now, question.id);
      this.news(agent, "question_answered", question.id, "Question answered", now); this.emit(question.sender, agent, "question_answered", question.id);
      return json({ id: question.id, answered_at: now });
    }
    if (path === "/v1/news" && method === "GET") {
      const last = this.one("SELECT last_id FROM news_seen WHERE agent=?", agent)?.last_id ?? 0;
      const unseen_count = Number(this.one("SELECT COUNT(*) AS count FROM news WHERE id>?", last).count);
      const news = this.rows("SELECT * FROM news WHERE id>? ORDER BY id LIMIT 100", last);
      if (url.searchParams.get("peek") !== "true" && news.length) this.sql("INSERT INTO news_seen(agent,last_id) VALUES (?,?) ON CONFLICT(agent) DO UPDATE SET last_id=excluded.last_id", agent, news[news.length - 1].id);
      return json({ news, unseen_count });
    }
    if (path === "/v1/commits" && method === "POST") {
      const subject = cleanText(body.subject, 200);
      if (typeof body.sha !== "string" || !/^[0-9a-f]{7,64}$/i.test(body.sha)) throw new HttpError(400, "sha must contain 7-64 hexadecimal characters");
      const branch = body.branch === undefined ? null : cleanText(body.branch, 200);
      const files = body.files === undefined ? [] : body.files;
      if (!Array.isArray(files) || files.length > 50) throw new HttpError(400, "files must contain at most 50 paths");
      const normalized = files.map(filePath);
      const summary = `Commit ${body.sha}${branch === null ? "" : ` [${branch}]`}: ${subject}${normalized.length ? `\nFiles: ${normalized.join(", ")}` : ""}`;
      const id = this.news(agent, "commit", body.sha, summary, now); this.broadcast(agent, "commit", body.sha);
      return json({ id, created_at: now }, 201);
    }
    return undefined;
  }
}
