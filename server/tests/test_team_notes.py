"""Home-server-only handoffs and shared notes retain history and team boundaries."""

import json
import re
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import pytest

from agentmesh_server.config import init_config
from agentmesh_server.hub import find_node
from test_hub_seed import running_hub


HUB = Path(__file__).resolve().parents[1] / "src/agentmesh_server/vendor/openhuddle/src/hub"
HANDOFF_TABLE = "task_handoffs"
NOTES_TABLE = "team_notes"


@pytest.fixture
def notes_api(tmp_path):
    directory = tmp_path / "team-notes"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        api.config_dir = directory
        yield api


def create_task(api, key="T1", role="a"):
    response = api.post("/api/plan", {"tasks": [{"key": key, "title": "Task for handoff"}]}, role)
    assert response.status == 200, response.body
    return response.json()["tasks"][0]["key"]


def handoff_payload(key="T1", summary="Review completed"):
    return {"task_key": key, "summary": summary, "changed_files": ["server/README.md"],
            "open_questions": ["Which release?"], "next_step": "Run verification"}


def create_note(api, title="Shared design", body="The team should review this", tags=None, role="a"):
    response = api.post("/api/notes", {"title": title, "body": body, "tags": tags or []}, role)
    assert response.status == 200, response.body
    return response.json()["note"]


def add_peer(api, name="Reader"):
    invite = api.post("/api/invite", {}).json()["invite_url"]
    response = api.post("/api/join", {"code": invite, "member_name": name}, None)
    assert response.status == 200, response.body
    return response.json()


AUTH_ROUTES = [
    ("POST", "/api/handoff", handoff_payload()),
    ("GET", "/api/handoffs?task_key=T1", None),
    ("POST", "/api/notes", {"title": "Test", "body": "Test", "tags": []}),
    ("GET", "/api/notes", None),
    ("DELETE", "/api/notes/1", None),
]


@pytest.mark.parametrize("role", [None, "invalid-member-token", "viewer"])
def test_every_new_route_requires_a_member_token(notes_api, role):
    for method, path, payload in AUTH_ROUTES:
        response = notes_api.request(method, path, role, data=payload)
        assert response.status == 401, (method, path, response.body)


def test_removed_member_cannot_use_any_new_route(notes_api):
    peer = add_peer(notes_api)
    assert notes_api.post("/api/members/remove", {"member": peer["member_id"]}).status == 200
    for method, path, payload in AUTH_ROUTES:
        response = notes_api.request(method, path, peer["token"], data=payload)
        assert response.status == 401, (method, path, response.body)


def test_handoffs_assignee_creator_readers_feed_and_latest_first(notes_api):
    api = notes_api
    key = create_task(api)
    assert api.post("/api/task", {"action": "take", "key": key}, "b").status == 200
    reader = add_peer(api)
    first = api.post("/api/handoff", handoff_payload(key), "b")
    assert first.status == 200, first.body
    first = first.json()["handoff"]
    assert first["author_id"] == api.get("/api/state", "b").json()["me"]["id"]
    assert {field: first[field] for field in handoff_payload(key)} == handoff_payload(key)

    second = api.post("/api/handoff", handoff_payload(key, "Creator adds context"))
    assert second.status == 200, second.body
    second = second.json()["handoff"]
    assert api.post("/api/handoff", handoff_payload(key), reader["token"]).status == 403
    assert api.get("/api/handoffs?task_key=" + key, reader["token"]).json() == {
        "handoffs": [second, first]}
    assert api.get("/api/handoffs", "b").json() == {"handoffs": [second, first]}
    events = api.get("/api/events?since=0", reader["token"]).json()["events"]
    recorded = [event for event in events if event["kind"] == "handoff_created"]
    assert [event["data"]["id"] for event in recorded] == [first["id"], second["id"]]
    assert [event["data"]["task_key"] for event in recorded] == [key, key]

    # Being team owner is not an exception to creator/assignee authorization.
    peer_key = create_task(api, "PEER", "b")
    assert api.post("/api/handoff", handoff_payload(peer_key)).status == 403
    assert api.post("/api/handoff", handoff_payload("MISSING")).status == 404


def test_handoffs_and_notes_are_scoped_even_when_task_keys_match(notes_api):
    api = notes_api
    create_task(api)
    local_handoff = api.post("/api/handoff", handoff_payload()).json()["handoff"]
    local_note = create_note(api)
    created = api.post("/api/teams", {"team_name": "Separate team", "member_name": "Owner"}, None)
    assert created.status == 200, created.body
    other = created.json()["token"]
    # A task key from another team must not authorize a handoff before a local task exists.
    assert api.post("/api/handoff", handoff_payload(), other).status == 404
    assert api.get("/api/handoffs?task_key=T1", other).json() == {"handoffs": []}
    assert api.get("/api/notes", other).json() == {"notes": []}
    assert api.request("DELETE", f"/api/notes/{local_note['id']}", other).status == 404
    create_task(api, role=other)
    other_handoff = api.post("/api/handoff", handoff_payload(summary="Other team"), other).json()["handoff"]
    other_note = create_note(api, title="Other team", role=other)
    assert api.get("/api/handoffs?task_key=T1").json() == {"handoffs": [local_handoff]}
    assert api.get("/api/handoffs", other).json() == {"handoffs": [other_handoff]}
    assert api.get("/api/notes").json() == {"notes": [local_note]}
    assert api.get("/api/notes", other).json() == {"notes": [other_note]}
    assert api.request("DELETE", f"/api/notes/{other_note['id']}").status == 404
    for token, hidden in (("a", other_handoff), (other, local_handoff)):
        events = api.get("/api/events?since=0", token).json()["events"]
        assert not any(event["kind"] == "handoff_created" and event["data"]["id"] == hidden["id"]
                       for event in events)


def test_notes_delete_is_explicit_author_only_and_removes_the_row(notes_api):
    api = notes_api
    # Even an owner cannot delete a different member's note.
    note = create_note(api, role="b")
    path = f"/api/notes/{note['id']}"
    assert api.get("/api/notes").json() == {"notes": [note]}
    assert api.request("DELETE", path).status == 403
    assert api.post(path, {"delete": True}, "b").status in (404, 405)
    assert api.get("/api/notes", "b").json() == {"notes": [note]}
    with sqlite3.connect(api.config_dir / "hub.db") as db:
        assert db.execute(f"SELECT count(*) FROM {NOTES_TABLE} WHERE id=?", (note["id"],)).fetchone() == (1,)
    deleted = api.request("DELETE", path, "b")
    assert deleted.status == 200, deleted.body
    assert deleted.json() == {"ok": True}
    assert api.get("/api/notes").json() == {"notes": []}
    assert api.request("DELETE", path, "b").status == 404
    with sqlite3.connect(api.config_dir / "hub.db") as db:
        assert db.execute(f"SELECT * FROM {NOTES_TABLE} WHERE id=?", (note["id"],)).fetchall() == []
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({NOTES_TABLE})")}
        assert not columns.intersection({"removed", "removed_at", "deleted", "deleted_at"})


def test_notes_search_tag_combination_and_literal_query_safety(notes_api):
    api = notes_api
    title_match = create_note(api, "Release design", "Needs review", ["release", "planning"])
    body_match = create_note(api, "Follow up", "RELEASE after validation", ["release"])
    create_note(api, "Design", "Needs review", ["design"])
    literal = create_note(api, "Literal 100%_ ' OR 1=1 --", "Punctuation", ["x' OR 1=1 --"])

    def ids(**query):
        response = api.get("/api/notes?" + urlencode(query), "b")
        assert response.status == 200, response.body
        return {note["id"] for note in response.json()["notes"]}

    assert ids(q="release") == {title_match["id"], body_match["id"]}
    assert ids(tag="release") == {title_match["id"], body_match["id"]}
    assert ids(q="design", tag="release") == {title_match["id"]}
    assert ids(q="release", tag="design") == set()
    assert ids(tag="relea") == set()
    assert ids(q="100%_") == {literal["id"]}
    assert ids(q="' OR 1=1 --") == {literal["id"]}
    assert ids(tag="x' OR 1=1 --") == {literal["id"]}
    assert len(api.get("/api/notes").json()["notes"]) == 4


@pytest.mark.parametrize("field,value", [
    ("title", "x" * 201), ("body", "x" * 8001), ("tags", [str(n) for n in range(11)]),
    ("title", None), ("title", 23), ("title", "  "), ("body", []), ("body", ""),
    ("tags", "release"), ("tags", ["release", 3]), ("tags", None),
])
def test_notes_reject_oversize_or_malformed_fields_without_writing(notes_api, field, value):
    payload = {"title": "Title", "body": "Body", "tags": []}
    payload[field] = value
    response = notes_api.post("/api/notes", payload)
    assert response.status == 400, response.body
    assert notes_api.get("/api/notes").json() == {"notes": []}


def test_notes_accept_exact_limits(notes_api):
    tags = [f"tag-{n}" for n in range(10)]
    note = create_note(notes_api, "t" * 200, "b" * 8000, tags)
    assert (len(note["title"]), len(note["body"]), note["tags"]) == (200, 8000, tags)


@pytest.mark.parametrize("field,value", [
    ("task_key", 4), ("task_key", "T1' OR 1=1"), ("summary", None), ("summary", []),
    ("summary", ""), ("summary", "x" * 8001), ("changed_files", "server/file.py"),
    ("changed_files", [1]), ("open_questions", {}), ("open_questions", [None]),
    ("next_step", []), ("next_step", "x" * 8001),
])
def test_handoffs_validate_fields_without_writing(notes_api, field, value):
    create_task(notes_api)
    payload = handoff_payload()
    payload[field] = value
    response = notes_api.post("/api/handoff", payload)
    assert response.status == 400, response.body
    assert notes_api.get("/api/handoffs").json() == {"handoffs": []}


def test_handoffs_and_notes_survive_full_restart_and_old_history_sweeps(tmp_path):
    directory = tmp_path / "durable-notes"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        team = api.get("/api/state").json()["team"]["id"]
        create_task(api)
        first = api.post("/api/handoff", handoff_payload()).json()["handoff"]
        second = api.post("/api/handoff", handoff_payload(summary="Newer handoff")).json()["handoff"]
        note = create_note(api, role="b")
        peer = api.get("/api/state", "b").json()["me"]["id"]
        # Neither removal of a member nor replacing the task board prunes its history.
        assert api.post("/api/members/remove", {"member": peer}).status == 200
        assert api.post("/api/plan", {"replace": True, "tasks": [{"key": "NEW", "title": "New plan"}]}).status == 200
        assert api.get("/api/handoffs?task_key=T1").json() == {"handoffs": [second, first]}
    database = directory / "hub.db"
    with sqlite3.connect(database) as db:
        for table in (HANDOFF_TABLE, NOTES_TABLE):
            db.execute(f"UPDATE {table} SET created_at=?", (1,))
        before = {table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                  for table in (HANDOFF_TABLE, NOTES_TABLE)}
    script = f"""
        import {{ openDb }} from {json.dumps((HUB / 'db.js').as_uri())};
        import {{ Hub }} from {json.dumps((HUB / 'hub.js').as_uri())};
        import {{ sweep }} from {json.dumps((HUB / 'huddles.js').as_uri())};
        const db = openDb(process.argv[1]);
        const hub = new Hub(db, {{ now: () => Date.UTC(2040, 0, 1), retentionDays: 1 }});
        sweep(hub); hub.sweep(); hub.sweep(); db.close();
    """
    result = subprocess.run([find_node(), "--input-type=module", "-e", script, str(database)],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(database) as db:
        assert {table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in (HANDOFF_TABLE, NOTES_TABLE)} == before
    with running_hub(directory, config) as api:
        assert api.get("/api/state").json()["team"]["id"] == team
        handoffs = api.get("/api/handoffs?task_key=T1").json()["handoffs"]
        assert [row["id"] for row in handoffs] == [second["id"], first["id"]]
        assert [row["summary"] for row in handoffs] == ["Newer handoff", "Review completed"]
        notes = api.get("/api/notes").json()["notes"]
        assert notes == [{**note, "created_at": 1}]
        assert all(row["created_at"] == 1 for row in handoffs)
        events = api.get("/api/events?since=0").json()["events"]
        assert len([event for event in events if event["kind"] == "handoff_created"]) == 2


def test_notes_migration_is_additive_and_preserves_schema_two_database(tmp_path):
    directory = tmp_path / "legacy-hub"
    config = init_config(directory)
    source = (HUB / "db.js").read_text()
    schema = re.search(r"const SCHEMA = `(.+?)`;", source, re.DOTALL)[1]
    huddles = re.search(r"const HUDDLE_TABLES = `(.+?)`;", source, re.DOTALL)[1]
    with sqlite3.connect(directory / "hub.db") as db:
        db.executescript(schema)
        db.execute("ALTER TABLE teams ADD COLUMN next_huddle INTEGER NOT NULL DEFAULT 1")
        db.executescript(huddles)
        db.execute("PRAGMA user_version=2")
        db.execute("INSERT INTO events (team_id,kind,data,created_at) VALUES ('archive','old','{}',1)")
        db.execute("INSERT INTO tasks (team_id,key,seq,title,status,created_at,updated_at) "
                   "VALUES ('archive','OLD',1,'Keep historical task','done',1,1)")
        old_schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    for _ in range(2):
        with running_hub(directory, config) as api:
            assert api.get("/api/notes").status == 200
            assert api.get("/api/handoffs").status == 200
            with sqlite3.connect(directory / "hub.db") as db:
                assert db.execute("PRAGMA user_version").fetchone() == (3,)
                for entry in old_schema:
                    assert db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=?",
                                      (entry[1],)).fetchone() == entry
                assert db.execute("SELECT title,status FROM tasks WHERE team_id='archive'").fetchall() == [
                    ("Keep historical task", "done")]
                assert db.execute("SELECT kind,data,created_at FROM events WHERE team_id='archive'").fetchall() == [
                    ("old", "{}", 1)]
                for table in (HANDOFF_TABLE, NOTES_TABLE):
                    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                                      (table,)).fetchone() == (table,)
