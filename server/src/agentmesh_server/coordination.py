"""Additive coordination state in the existing SQLite store.

All functions run under the server condition lock; mutations use the existing
request transaction. Legacy transport validation intentionally remains separate.
"""

import hashlib
import json
import re
import uuid

from .state import AGENTS, DAY

JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
ALL_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
HUDDLE_PATH = re.compile(r"/v1/huddles/([0-9a-f-]{36})(?:/(posts|close))?", re.IGNORECASE)
QUESTION_PATH = re.compile(r"/v1/questions/([0-9a-f-]{36})/answer", re.IGNORECASE)
SCHEMA = """
CREATE TABLE IF NOT EXISTS master_sections (
 section TEXT PRIMARY KEY,text TEXT NOT NULL,author TEXT NOT NULL,revision INTEGER NOT NULL,
 sha256 TEXT NOT NULL,created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS master_edits (
 id TEXT PRIMARY KEY,section TEXT NOT NULL,text TEXT NOT NULL,author TEXT NOT NULL,
 revision INTEGER NOT NULL,sha256 TEXT NOT NULL,created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS master_state (
 id INTEGER PRIMARY KEY CHECK(id=1),status TEXT NOT NULL DEFAULT 'draft',
 revision INTEGER NOT NULL DEFAULT 0,locked_version INTEGER NOT NULL DEFAULT 0,
 round INTEGER NOT NULL DEFAULT 0,lead TEXT
);
INSERT OR IGNORE INTO master_state (id) VALUES (1);
CREATE TABLE IF NOT EXISTS master_votes (
 round INTEGER NOT NULL,agent TEXT NOT NULL,decision TEXT NOT NULL,note TEXT,
 PRIMARY KEY(round,agent)
);
CREATE TABLE IF NOT EXISTS file_claims (
 path TEXT PRIMARY KEY,agent TEXT NOT NULL,task_id TEXT,expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS huddles (
 id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,topic TEXT NOT NULL,
 status TEXT NOT NULL,decision TEXT,created_at INTEGER NOT NULL,closed_at INTEGER
);
CREATE TABLE IF NOT EXISTS huddle_posts (
 id TEXT PRIMARY KEY,huddle_id TEXT NOT NULL,author TEXT NOT NULL,kind TEXT NOT NULL,
 text TEXT NOT NULL,ref_post_id TEXT,created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
 id TEXT PRIMARY KEY,sender TEXT NOT NULL,recipient TEXT NOT NULL,text TEXT NOT NULL,
 task_id TEXT,answer TEXT,created_at INTEGER NOT NULL,answered_at INTEGER
);
CREATE TABLE IF NOT EXISTS news (
 id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,kind TEXT NOT NULL,
 ref TEXT NOT NULL,summary TEXT NOT NULL,created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS news_seen (agent TEXT PRIMARY KEY,last_id INTEGER NOT NULL);
"""


class CoordinationError(Exception):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.status = status
        self.details = details


def clean_text(value, maximum=8000):
    if not isinstance(value, str):
        raise CoordinationError(400, "text must be a string")
    value = CONTROL.sub("", value).strip(JS_WHITESPACE)
    if not value or len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > maximum:
        raise CoordinationError(400, f"text must be non-empty and at most {maximum} UTF-16 units")
    # Lone surrogate code points cannot be stored as UTF-8 in SQLite.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise CoordinationError(400, "text must contain valid Unicode")
    return value


def clean_path(value):
    if not isinstance(value, str) or ALL_CONTROL.search(value):
        raise CoordinationError(400, "path must not contain control characters")
    value = clean_text(value, 500).replace("\\", "/")
    if value.startswith("/") or re.match(r"^[a-zA-Z]:", value):
        raise CoordinationError(400, "path must be repo-relative")
    parts = value.split("/")
    if ".." in parts:
        raise CoordinationError(400, "path must not contain ..")
    value = "/".join(part for part in parts if part not in ("", "."))
    if not value:
        raise CoordinationError(400, "path must name a file")
    return value


def migrate(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    for name, kind in (("owner", "TEXT"), ("file_areas", "TEXT"),
                       ("depends_on", "TEXT"), ("contract", "INTEGER DEFAULT 0")):
        if name not in columns:
            db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
    db.executescript(SCHEMA)


def sweep(db, now):
    removed = {}
    removed["file_claims"] = db.execute("DELETE FROM file_claims WHERE expires_at<=?", (now,)).rowcount
    removed["news"] = db.execute("DELETE FROM news WHERE created_at<?", (now - 30 * DAY,)).rowcount
    db.execute("DELETE FROM huddle_posts WHERE huddle_id IN (SELECT id FROM huddles WHERE status='closed' AND closed_at<?)", (now - 90 * DAY,))
    removed["huddles"] = db.execute("DELETE FROM huddles WHERE status='closed' AND closed_at<?", (now - 90 * DAY,)).rowcount
    removed["questions"] = db.execute("DELETE FROM questions WHERE answered_at IS NOT NULL AND answered_at<?", (now - 30 * DAY,)).rowcount
    return removed


def news(state, actor, kind, ref, summary, now):
    summary = summary.encode("utf-16-le")[:16000].decode("utf-16-le", errors="ignore")
    return state.db.execute("INSERT INTO news (actor,kind,ref,summary,created_at) VALUES (?,?,?,?,?)",
                            (actor, kind, ref, summary, now)).lastrowid


def emit(state, recipient, actor, kind, ref, now):
    """New coordination events may deliberately notify their actor too."""
    state.db.execute("INSERT INTO events (recipient,type,ref,actor,created_at) VALUES (?,?,?,?,?)",
                     (recipient, kind, ref, actor, now))
    state.condition.notify_all()


def changed(state, actor, kind, ref, summary, now, everyone=False):
    news_id = news(state, actor, kind, ref, summary, now)
    for member in AGENTS:
        if everyone or member != actor:
            emit(state, member, actor, kind, ref, now)
    return news_id


def waiting_on(state, dependencies):
    unfinished = []
    for task_id in dependencies:
        found = state.rows("SELECT status FROM tasks WHERE id=?", task_id)
        if not found or found[0]["status"] not in ("done", "accepted"):
            unfinished.append(task_id)
    return unfinished


def task_fields(state, row):
    row["owner"] = row.get("owner")
    row["file_areas"] = json.loads(row.get("file_areas") or "[]")
    row["depends_on"] = json.loads(row.get("depends_on") or "[]")
    row["contract"] = bool(row.get("contract"))
    row["waiting_on"] = waiting_on(state, row["depends_on"])
    return row


def visible_task(state, agent, task_id):
    if not isinstance(task_id, str) or not UUID.fullmatch(task_id):
        raise CoordinationError(400, "task_id must be a UUID")
    found = state.rows("SELECT * FROM tasks WHERE id=? AND (created_by=? OR assignee=?)", task_id, agent, agent)
    if not found:
        raise CoordinationError(400, "task must be visible to caller")
    return task_id


def task_options(state, agent, body):
    owner = body.get("owner", body.get("assignee"))
    if owner not in AGENTS:
        raise CoordinationError(400, "owner must be agent-a or agent-b")
    areas = body.get("file_areas", [])
    dependencies = body.get("depends_on", [])
    if not isinstance(areas, list) or len(areas) > 20:
        raise CoordinationError(400, "file_areas must contain at most 20 paths")
    areas = [clean_path(clean_text(area, 500)) for area in areas]
    if not isinstance(dependencies, list) or len(dependencies) > 20:
        raise CoordinationError(400, "depends_on must contain at most 20 task ids")
    dependencies = list(dict.fromkeys(visible_task(state, agent, item) for item in dependencies))
    contract = body.get("contract", False)
    if not isinstance(contract, bool):
        raise CoordinationError(400, "contract must be a boolean")
    return owner, areas, dependencies, contract


def save_task_options(state, task_id, options):
    owner, areas, dependencies, contract = options
    state.db.execute("UPDATE tasks SET owner=?,file_areas=?,depends_on=?,contract=? WHERE id=?",
                     (owner, json.dumps(areas), json.dumps(dependencies), int(contract), task_id))


def unblocked(state, actor, task_id, now):
    for task in state.rows("SELECT * FROM tasks WHERE status IN ('open','in_progress','blocked')"):
        dependencies = json.loads(task.get("depends_on") or "[]")
        if task_id in dependencies and not waiting_on(state, dependencies):
            owner = task.get("owner") or task["assignee"]
            emit(state, owner, actor, "task_unblocked", task["id"], now)
            news(state, actor, "task_unblocked", task["id"], "Task dependencies satisfied", now)


def _new_task(state, agent, item, now, dependencies=None, contract=False, owner=None):
    if not isinstance(item, dict):
        raise CoordinationError(400, "tasks must contain objects")
    title, description = clean_text(item.get("title"), 200), clean_text(item.get("description"))
    if item.get("assignee") not in AGENTS:
        raise CoordinationError(400, "assignee must be agent-a or agent-b")
    criteria = item.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or len(criteria) > 20:
        raise CoordinationError(400, "acceptance_criteria must contain at most 20 strings")
    criteria = [clean_text(value, 500) for value in criteria]
    body = dict(item, owner=owner if owner is not None else item["assignee"],
                depends_on=dependencies or [], contract=contract)
    options = task_options(state, agent, body)
    task_id = str(uuid.uuid4())
    state.db.execute("INSERT INTO tasks (id,title,description,assignee,created_by,status,acceptance_criteria,result,created_at,updated_at) VALUES (?,?,?,?,?,'open',?,NULL,?,?)",
                     (task_id, title, description, item["assignee"], agent, json.dumps(criteria), now, now))
    save_task_options(state, task_id, options)
    state.emit(item["assignee"], agent, "task_assigned", task_id)
    news(state, agent, "task_created", task_id, "Task created", now)
    return task_id


def master(state):
    current = state.rows("SELECT status,revision,locked_version,round,lead FROM master_state WHERE id=1")[0]
    votes = state.rows("SELECT * FROM master_votes WHERE round=? ORDER BY agent", current["round"])
    approved = {vote["agent"] for vote in votes if vote["decision"] == "approve"}
    return {"state": current,
            "sections": state.rows("SELECT * FROM master_sections ORDER BY section"),
            "votes": votes,
            "waiting_on": [agent for agent in AGENTS if agent not in approved] if current["status"] == "voting" else [],
            "edits": state.rows("SELECT * FROM master_edits ORDER BY revision DESC LIMIT 50")[::-1]}


def huddle(state, row):
    posts = state.rows("SELECT * FROM huddle_posts WHERE huddle_id=? ORDER BY created_at,rowid", row["id"])
    row["posts"] = posts
    posted = {post["author"] for post in posts}
    row["waiting_on"] = [agent for agent in AGENTS if agent not in posted] if row["status"] == "open" else []
    return row


def snapshot(state, now):
    return {"master_prompt": master(state),
            "huddles": [huddle(state, row) for row in state.rows("SELECT * FROM huddles ORDER BY created_at DESC LIMIT 100")],
            "claims": state.rows("SELECT * FROM file_claims WHERE expires_at>? ORDER BY path", now),
            "questions": state.rows("SELECT * FROM questions ORDER BY created_at DESC LIMIT 200")[::-1],
            "news": state.rows("SELECT * FROM news ORDER BY id DESC LIMIT 200")[::-1]}


def route(handler, state, agent, path, query, now):
    """Return a response tuple, or None to retain the legacy route dispatcher."""
    method, db, rows = handler.command, state.db, state.rows
    def param(name):
        return query.get(name, [None])[0]

    if path == "/v1/master" and method == "GET":
        return master(state), 200
    if path in ("/v1/master/sections", "/v1/master/submit", "/v1/master/vote") and method == "POST":
        current = master(state)["state"]
        body = handler._body_json()
        if path.endswith("/sections"):
            if current["status"] == "voting":
                raise CoordinationError(409, "master prompt is voting")
            section, text = clean_text(body.get("section"), 80), clean_text(body.get("text"))
            revision = current["revision"] + 1
            digest = hashlib.sha256(text.encode()).hexdigest()
            db.execute("INSERT INTO master_sections (section,text,author,revision,sha256,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET text=excluded.text,author=excluded.author,revision=excluded.revision,sha256=excluded.sha256,created_at=excluded.created_at",
                       (section, text, agent, revision, digest, now))
            db.execute("INSERT INTO master_edits (id,section,text,author,revision,sha256,created_at) VALUES (?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), section, text, agent, revision, digest, now))
            db.execute("UPDATE master_state SET status='draft',revision=?,lead=COALESCE(lead,?) WHERE id=1", (revision, agent))
            changed(state, agent, "master_edited", section, "Master prompt section edited: " + section, now)
            return {"revision": revision, "sha256": digest, "author": agent}, 201
        if path.endswith("/submit"):
            if current["status"] != "draft":
                raise CoordinationError(409, "master prompt must be draft")
            if current["revision"] == 0:
                raise CoordinationError(409, "master prompt has no sections")
            vote_round = current["round"] + 1
            db.execute("UPDATE master_state SET status='voting',round=? WHERE id=1", (vote_round,))
            db.execute("INSERT INTO master_votes (round,agent,decision,note) VALUES (?,?,'approve',NULL)", (vote_round, agent))
            changed(state, agent, "master_submitted", str(vote_round), "Master prompt submitted for vote", now)
        else:
            if current["status"] != "voting":
                raise CoordinationError(409, "master prompt is not voting")
            decision = body.get("decision")
            if decision not in ("approve", "reject"):
                raise CoordinationError(400, "decision must be approve or reject")
            note = clean_text(body["note"]) if "note" in body else None
            db.execute("INSERT INTO master_votes (round,agent,decision,note) VALUES (?,?,?,?) ON CONFLICT(round,agent) DO UPDATE SET decision=excluded.decision,note=excluded.note", (current["round"], agent, decision, note))
            changed(state, agent, "master_voted", str(current["round"]), "Master prompt vote recorded", now)
            if decision == "reject":
                db.execute("UPDATE master_state SET status='draft' WHERE id=1")
            elif db.execute("SELECT COUNT(*) FROM master_votes WHERE round=? AND decision='approve'", (current["round"],)).fetchone()[0] == len(AGENTS):
                db.execute("UPDATE master_state SET status='locked',locked_version=locked_version+1 WHERE id=1")
                changed(state, agent, "master_locked", str(current["round"]), "Master prompt locked", now)
        return master(state), 200

    if path == "/v1/tasks/propose" and method == "POST":
        current = master(state)["state"]
        if current["lead"] != agent:
            raise CoordinationError(403, "only the master prompt lead may propose tasks")
        if current["status"] != "locked":
            raise CoordinationError(409, "master prompt must be locked")
        items = handler._body_json().get("tasks")
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise CoordinationError(400, "tasks must contain 1 to 20 objects")
        ids = []
        for index, item in enumerate(items):
            ids.append(_new_task(state, agent, item, now, ids[:1], index == 0))
        tasks = []
        for task_id in ids:
            task = task_fields(state, rows("SELECT * FROM tasks WHERE id=?", task_id)[0])
            task["acceptance_criteria"] = json.loads(task["acceptance_criteria"])
            tasks.append(task)
        return {"tasks": tasks}, 201

    if path == "/v1/claims" and method == "GET":
        db.execute("DELETE FROM file_claims WHERE expires_at<=?", (now,))
        target = clean_path(param("path")) if "path" in query else None
        return {"agent": agent, "claims": rows("SELECT * FROM file_claims WHERE expires_at>?" + (" AND path=?" if target is not None else "") + " ORDER BY path", now, *((target,) if target is not None else ()))}, 200
    if path in ("/v1/claims", "/v1/claims/release") and method == "POST":
        body = handler._body_json()
        target = clean_path(body.get("path"))
        db.execute("DELETE FROM file_claims WHERE expires_at<=?", (now,))
        held = rows("SELECT * FROM file_claims WHERE path=? AND expires_at>?", target, now)
        if held and held[0]["agent"] != agent:
            raise CoordinationError(403 if path.endswith("/release") else 409, "file is claimed by another member", held_by=held[0]["agent"], expires_at=held[0]["expires_at"])
        if path.endswith("/release"):
            deleted = db.execute("DELETE FROM file_claims WHERE path=? AND agent=?", (target, agent)).rowcount
            if deleted:
                changed(state, agent, "file_released", target, "File claim released: " + target, now)
            return {"ok": True, "changed": deleted}, 200
        task_id = body.get("task_id")
        task_id = visible_task(state, agent, task_id) if task_id is not None else None
        db.execute("INSERT INTO file_claims (path,agent,task_id,expires_at,created_at) VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET agent=excluded.agent,task_id=excluded.task_id,expires_at=excluded.expires_at,created_at=excluded.created_at", (target, agent, task_id, now + 30 * 60 * 1000, now))
        changed(state, agent, "file_claimed", target, "File claimed: " + target, now)
        return rows("SELECT * FROM file_claims WHERE path=?", target)[0], 201

    if path == "/v1/huddles" and method == "GET":
        status = param("status")
        if status is not None and status not in ("open", "closed"):
            raise CoordinationError(400, "status must be open or closed")
        selected = rows("SELECT * FROM huddles" + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC LIMIT 100", *((status,) if status else ()))
        return {"huddles": [huddle(state, item) for item in selected]}, 200
    if path == "/v1/huddles" and method == "POST":
        body = handler._body_json()
        if body.get("kind") not in ("brainstorm", "design", "review", "standup"):
            raise CoordinationError(400, "invalid huddle kind")
        topic, huddle_id = clean_text(body.get("topic"), 200), str(uuid.uuid4())
        db.execute("INSERT INTO huddles (id,owner,kind,topic,status,created_at) VALUES (?,?,?,?,'open',?)", (huddle_id, agent, body["kind"], topic, now))
        changed(state, agent, "huddle_started", huddle_id, "Huddle started: " + topic, now)
        return {"id": huddle_id, "created_at": now}, 201
    match = HUDDLE_PATH.fullmatch(path)
    if match:
        huddle_id, action = match.groups()
        found = rows("SELECT * FROM huddles WHERE id=?", huddle_id)
        if not found:
            raise CoordinationError(404, "huddle not found")
        current = found[0]
        if method == "GET" and action is None:
            return {"huddle": huddle(state, current)}, 200
        if method != "POST" or action not in ("posts", "close"):
            raise CoordinationError(405, "method not allowed")
        if action == "close" and current["owner"] != agent:
            raise CoordinationError(403, "only the owner may close a huddle")
        if current["status"] != "open":
            raise CoordinationError(409, "huddle is closed")
        body = handler._body_json()
        if action == "posts":
            if body.get("kind") not in ("idea", "back", "push_back"):
                raise CoordinationError(400, "invalid post kind")
            text, ref = clean_text(body.get("text")), body.get("ref_post_id")
            if ref is not None and (not isinstance(ref, str) or not UUID.fullmatch(ref) or not rows("SELECT id FROM huddle_posts WHERE id=? AND huddle_id=?", ref, huddle_id)):
                raise CoordinationError(400, "ref_post_id must refer to this huddle")
            post_id = str(uuid.uuid4())
            db.execute("INSERT INTO huddle_posts (id,huddle_id,author,kind,text,ref_post_id,created_at) VALUES (?,?,?,?,?,?,?)", (post_id, huddle_id, agent, body["kind"], text, ref, now))
            changed(state, agent, "huddle_posted", huddle_id, "Huddle contribution: " + current["topic"], now)
            return {"id": post_id, "created_at": now}, 201
        decision, items = clean_text(body.get("decision")), body.get("tasks", [])
        if not isinstance(items, list) or len(items) > 10:
            raise CoordinationError(400, "tasks must contain at most 10 objects")
        tasks = [_new_task(state, agent, item, now, owner=agent) for item in items]
        db.execute("UPDATE huddles SET status='closed',decision=?,closed_at=? WHERE id=?", (decision, now, huddle_id))
        changed(state, agent, "huddle_closed", huddle_id, "Huddle closed: " + current["topic"] + " — " + decision, now, everyone=True)
        return {"id": huddle_id, "status": "closed", "tasks": tasks}, 200

    if path == "/v1/questions" and method == "GET":
        return {"questions": rows("SELECT * FROM questions WHERE (sender=? OR recipient=?)" + (" AND answered_at IS NULL" if param("open") == "true" else "") + " ORDER BY created_at DESC LIMIT 100", agent, agent)[::-1]}, 200
    if path == "/v1/questions" and method == "POST":
        body = handler._body_json()
        if body.get("to") not in AGENTS or body["to"] == agent:
            raise CoordinationError(400, "to must be the other agent")
        text, question_id = clean_text(body.get("text")), str(uuid.uuid4())
        task_id = body.get("task_id")
        task_id = visible_task(state, agent, task_id) if task_id is not None else None
        db.execute("INSERT INTO questions (id,sender,recipient,text,task_id,created_at) VALUES (?,?,?,?,?,?)", (question_id, agent, body["to"], text, task_id, now))
        changed(state, agent, "question_asked", question_id, "Question asked", now)
        return {"id": question_id, "created_at": now}, 201
    match = QUESTION_PATH.fullmatch(path)
    if match and method == "POST":
        found = rows("SELECT * FROM questions WHERE id=?", match[1])
        if not found:
            raise CoordinationError(404, "question not found")
        if found[0]["recipient"] != agent:
            raise CoordinationError(403, "only the recipient may answer")
        if found[0]["answered_at"] is not None:
            raise CoordinationError(409, "question is already answered")
        text = clean_text(handler._body_json().get("text"))
        db.execute("UPDATE questions SET answer=?,answered_at=? WHERE id=?", (text, now, match[1]))
        changed(state, agent, "question_answered", match[1], "Question answered", now)
        return {"id": match[1], "answered_at": now}, 200

    if path == "/v1/news" and method == "GET":
        cursor = rows("SELECT last_id FROM news_seen WHERE agent=?", agent)
        after = cursor[0]["last_id"] if cursor else 0
        items = rows("SELECT * FROM news WHERE id>? ORDER BY id LIMIT 100", after)
        count = db.execute("SELECT COUNT(*) FROM news WHERE id>?", (after,)).fetchone()[0]
        if param("peek") != "true" and items:
            db.execute("INSERT INTO news_seen (agent,last_id) VALUES (?,?) ON CONFLICT(agent) DO UPDATE SET last_id=excluded.last_id", (agent, items[-1]["id"]))
        return {"news": items, "unseen_count": count}, 200
    if path == "/v1/commits" and method == "POST":
        body = handler._body_json()
        sha = body.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            raise CoordinationError(400, "sha must contain 7 to 64 hexadecimal characters")
        subject = clean_text(body.get("subject"), 200)
        branch = clean_text(body["branch"], 200) if "branch" in body else None
        paths = body.get("files", [])
        if not isinstance(paths, list) or len(paths) > 50:
            raise CoordinationError(400, "files must contain at most 50 paths")
        paths = [clean_path(item) for item in paths]
        summary = "Commit " + sha + (" [" + branch + "]" if branch else "") + ": " + subject
        if paths:
            summary += "\nFiles: " + ", ".join(paths)
        news_id = changed(state, agent, "commit", sha, summary, now)
        return {"id": news_id, "created_at": now}, 201
    return None
