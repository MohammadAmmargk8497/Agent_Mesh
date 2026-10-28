"""Unified page delivery and member-key isolation for legacy read adapters."""

import json
import re
import sqlite3
import uuid
from importlib.resources import files

import pytest

from agentmesh_server.config import init_config
from agentmesh_server.state import now_ms
from test_hub_seed import running_hub


@pytest.fixture
def unified_api(tmp_path):
    directory = tmp_path / "unified"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        api.config_dir = directory
        yield api


def test_same_dashboard_at_team_root_and_dashboard_root(unified_api):
    api = unified_api
    for path in ("/", "/t/texample"):
        result = api.get(path, None)
        assert result.status == 200
        nonce = re.search(r"script-src 'nonce-([^']+)'", result.headers["content-security-policy"])[1]
        template = files("agentmesh_server").joinpath("dashboard.html").read_text(encoding="utf-8")
        text = result.body.decode()
        assert text == template.replace("__AGENTMESH_NONCE__", nonce)
        assert not re.search("openhuddle", text, re.I)
        assert "read-only" not in text.lower()
        assert not re.search(r'<option[^>]+value=[\"\']viewer[\"\']', text, re.I)
        assert all(token not in text for token in api.tokens.values())
        head = api.request("HEAD", path, None)
        assert head.status == 200
        assert head.body == b""
    assert api.server.hub.process is None, "page delivery must not start the hub subprocess"


@pytest.mark.parametrize("path", ["/viewer", "/index.html"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_obsolete_dashboard_urls_redirect_without_starting_hub(unified_api, path, method):
    result = unified_api.request(method, path, None)
    assert result.status == 302
    assert result.headers["location"] == "/"
    assert result.body == b""
    assert unified_api.server.hub.process is None


def test_default_team_generated_key_reads_legacy_snapshot_and_patch(unified_api):
    api = unified_api
    state = api.get("/api/state").json()
    team = state["team"]["id"]
    invite = api.post("/api/invite", {}).json()["invite_url"]
    joined = api.post("/api/join", {"code": invite, "member_name": "Observer"}, None).json()
    message = api.post("/v1/messages", {"to": "agent-b", "text": "default-team-only"}).json()
    patch = api.post("/v1/patches", {"title": "Private patch", "text": "private diff"}).json()
    private_task = api.task(assignee="agent-b", role="b")
    private_message = str(uuid.uuid4())
    with api.server.state.condition:
        api.server.state.db.execute(
            "INSERT INTO messages (id,sender,recipient,text,created_at) VALUES (?,'agent-b','agent-b','Team transcript note',?)",
            (private_message, now_ms()),
        )
    agent_snapshot = api.get("/v1/dashboard/state").json()
    assert agent_snapshot["team_transcript"] is True
    assert private_task not in {row["id"] for row in agent_snapshot["tasks"]}
    assert private_message not in {row["id"] for row in agent_snapshot["messages"]}
    # Resolving the seeded identity must not fake agent-a's presence, even for a
    # generated member's first adapter request.
    with sqlite3.connect(api.config_dir / "hub.db") as database:
        database.execute("UPDATE members SET last_seen=1 WHERE id=?", (state["me"]["id"],))
    route = f"/api/legacy/transcript?team={team}"
    result = api.get(route, joined["token"])
    assert result.status == 200, result.body
    snapshot = result.json()
    assert {"messages", "tasks", "patches", "audit", "events", "last_event_id",
            "master_prompt", "huddles", "claims", "questions", "news"} <= set(snapshot)
    assert any(row["id"] == message["id"] for row in snapshot["messages"])
    assert any(row["id"] == patch["id"] for row in snapshot["patches"])
    assert private_task in {row["id"] for row in snapshot["tasks"]}
    assert private_message in {row["id"] for row in snapshot["messages"]}
    assert snapshot["audit"]
    assert api.get(f"/api/legacy/patches?team={team}", joined["token"]).json()["patches"][0]["id"] == patch["id"]
    assert api.get(f"/api/legacy/patches/{patch['id']}?team={team}", joined["token"]).json()["patch"]["text"] == "private diff"
    with sqlite3.connect(api.config_dir / "hub.db") as database:
        assert database.execute("SELECT last_seen FROM members WHERE id=?", (state["me"]["id"],)).fetchone()[0] == 1
    for invalid in ("not-an-integer", "1.5", "", "1e3", "9007199254740992"):
        assert api.get(route + "&after=" + invalid, joined["token"]).status == 400
    cursor = snapshot["last_event_id"]
    assert api.get(route + f"&after={cursor}", joined["token"]).json()["events"] == []
    sent = api.post("/v1/messages", {"to": "agent-a", "text": "next event"}, "b").json()
    later = api.get(route + f"&after={cursor}", joined["token"]).json()
    assert len(later["events"]) == 1
    assert later["events"][0]["actor"] == "agent-b"
    assert later["events"][0]["ref"] == sent["id"]
    assert api.get("/v1/transcript", "a").status == 403
    assert api.get("/v1/transcript", "viewer").status == 401
    assert api.post("/api/members/remove", {"member": joined["member_id"]}).status == 200
    assert api.get(route, joined["token"]).status == 401


def test_other_team_and_spoofed_default_names_cannot_read_legacy(unified_api):
    api = unified_api
    team = api.get("/api/state").json()["team"]["id"]
    other = api.post("/api/teams", {"team_name": "default", "member_name": "agent-a"}, None).json()
    patch = api.post("/v1/patches", {"title": "Private patch", "text": "default-only"}).json()["id"]
    for suffix in ("/transcript", "/patches", f"/patches/{patch}"):
        own = api.get(f"/api/legacy{suffix}?team={other['team_id']}", other["token"])
        if suffix == "/transcript":
            assert own.status == 200
            assert own.json()["messages"] == []
            assert own.json()["patches"] == []
            assert own.json()["audit"] == []
        else:
            assert own.status == 403
        assert api.get(f"/api/legacy{suffix}?team={team}", other["token"],
                       headers={"X-Agentmesh-Agent": "viewer"}).status == 403
        assert api.get(f"/api/legacy{suffix}?team={other['team_id']}").status == 403
    route = f"/api/legacy/transcript?team={team}"
    assert api.get(route, None).status == 401
    assert api.get(route, "viewer").status == 401
    assert api.get("/api/legacy/transcript").status == 403
    assert api.post(route, {}).status == 405
    assert api.get(f"/api/legacy/tasks?team={team}").status == 404


def _hub_domain(database):
    """Authentication presence may change; persisted message sources must not."""
    return {table: database.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("asks", "huddle_posts", "prompt_versions", "events")}


def test_transcript_merges_every_sent_message_in_order_without_writing(unified_api):
    api = unified_api
    member = api.get("/api/state").json()
    team = member["team"]["id"]
    direct = api.post("/v1/messages", {"to": "agent-b", "text": "direct text"}).json()
    ask = api.post("/api/ask", {"to": "agent-b", "question": "question text", "wait_s": 0}).json()["ask_id"]
    for answer in ("first answer", "corrected answer"):
        assert api.post("/api/reply", {"ask_id": ask, "answer": answer}, "b").status == 200
    proposal = "Complete idea " + "x" * 700
    huddle = api.post("/api/huddle/start", {"topic": "Human conversation", "proposal": proposal}).json()["huddle"]["id"]
    comment = "<img src=x onerror=alert(1)> " + "comment " * 100
    assert api.post("/api/huddle/post", {"id": huddle, "kind": "comment", "text": comment}, "b").status == 200
    assert api.post("/api/post", {"kind": "update", "text": "team-wide post"}).status == 200
    with api.server.state.condition:
        api.server.state.db.execute("UPDATE messages SET created_at=100 WHERE id=?", (direct["id"],))
    with sqlite3.connect(api.config_dir / "hub.db") as database:
        database.execute("UPDATE asks SET created_at=200,answered_at=400 WHERE team_id=?", (team,))
        database.execute("UPDATE events SET created_at=300 WHERE team_id=? AND kind='reply' AND json_extract(data,'$.answer')='first answer'", (team,))
        database.execute("UPDATE events SET created_at=400 WHERE team_id=? AND kind='reply' AND json_extract(data,'$.answer')='corrected answer'", (team,))
        database.execute("UPDATE huddle_posts SET created_at=400+seq*100 WHERE team_id=?", (team,))
        database.execute("UPDATE events SET created_at=700 WHERE team_id=? AND kind='update'", (team,))
        before = _hub_domain(database)
    result = api.get(f"/api/legacy/transcript?team={team}")
    assert result.status == 200, result.body
    messages = result.json()["messages"]
    assert [item["text"] for item in messages] == [
        "direct text", "question text", "first answer", "corrected answer",
        proposal, comment.strip(), "team-wide post",
    ]
    assert [item["kind"] for item in messages] == [
        "direct", "ask", "answer", "answer", "huddle post", "huddle comment", "huddle post",
    ]
    assert [item["created_at"] for item in messages] == list(range(100, 800, 100))
    assert len({item["id"] for item in messages}) == len(messages)
    assert messages[0]["sender"] == messages[1]["sender"] == "agent-a"
    assert messages[0]["recipient"] == messages[1]["recipient"] == "agent-b"
    assert messages[2]["sender"] == "agent-b" and messages[2]["recipient"] == "agent-a"
    assert all(item["ask_id"] == ask for item in messages[1:4])
    assert all(item["huddle_id"] == huddle and item["recipient"] == "team" for item in messages[4:6])
    assert messages[-1]["recipient"] == "team"
    with sqlite3.connect(api.config_dir / "hub.db") as database:
        assert _hub_domain(database) == before


def test_transcript_teams_with_reused_question_and_huddle_ids_are_isolated(unified_api):
    api = unified_api
    default = api.get("/api/state").json()["team"]["id"]
    api.post("/v1/messages", {"to": "agent-b", "text": "default direct"})
    assert api.post("/api/ask", {"to": "agent-b", "question": "default question", "wait_s": 0}).json()["ask_id"] == "Q1"
    assert api.post("/api/reply", {"ask_id": "Q1", "answer": "default answer"}, "b").status == 200
    assert api.post("/api/huddle/start", {"topic": "Default discussion", "proposal": "default huddle"}).json()["huddle"]["id"] == "H1"
    other = api.post("/api/teams", {"team_name": "Other", "member_name": "Other owner"}, None).json()
    invite = api.post("/api/invite", {}, other["token"]).json()["invite_url"]
    teammate = api.post("/api/join", {"code": invite, "member_name": "Other member"}, None).json()
    assert api.post("/api/ask", {"to": "Other member", "question": "other question", "wait_s": 0}, other["token"]).json()["ask_id"] == "Q1"
    assert api.post("/api/reply", {"ask_id": "Q1", "answer": "other answer"}, teammate["token"]).status == 200
    assert api.post("/api/huddle/start", {"topic": "Other discussion", "proposal": "other huddle"}, other["token"]).json()["huddle"]["id"] == "H1"
    assert api.post("/api/huddle/post", {"id": "H1", "kind": "comment", "text": "other comment"}, teammate["token"]).status == 200
    assert api.post("/api/post", {"kind": "decision", "text": "other team post"}, other["token"]).status == 200
    own_route = f"/api/legacy/transcript?team={other['team_id']}"
    own = api.get(own_route, teammate["token"])
    assert own.status == 200, own.body
    assert {item["text"] for item in own.json()["messages"]} == {
        "other question", "other answer", "other huddle", "other comment", "other team post",
    }
    for key in ("tasks", "patches", "audit", "events", "huddles", "claims", "questions", "news"):
        assert own.json()[key] == []
    default_text = {item["text"] for item in api.get(f"/api/legacy/transcript?team={default}").json()["messages"]}
    assert default_text == {"default direct", "default question", "default answer", "default huddle"}
    assert api.get(f"/api/legacy/transcript?team={default}", teammate["token"]).status == 403
    assert api.get(own_route).status == 403


def test_transcript_keeps_newest_500_with_stable_equal_time_order(unified_api):
    api = unified_api
    member = api.get("/api/state").json()
    team, sender = member["team"]["id"], member["me"]["id"]
    with api.server.state.condition:
        api.server.state.db.executemany(
            "INSERT INTO messages (id,sender,recipient,text,created_at) VALUES (?,'agent-a','agent-b',?,?)",
            [(str(uuid.uuid4()), f"direct-{index}", index * 2) for index in range(260)],
        )
    with sqlite3.connect(api.config_dir / "hub.db") as database:
        database.executemany(
            "INSERT INTO events (team_id,member_id,kind,data,created_at) VALUES (?,?,'update',?,?)",
            [(team, sender, json.dumps({"text": f"team-{index}"}), index * 2 + 1) for index in range(260)],
        )
        # IDs crossing 9/10 must retain numeric event order when timestamps tie.
        database.executemany(
            "INSERT INTO events (id,team_id,member_id,kind,data,created_at) VALUES (?,?,?,'update',?,600)",
            [(990 + index, team, sender, json.dumps({"text": f"tied-{index}"})) for index in range(12)],
        )
    route = f"/api/legacy/transcript?team={team}"
    first = api.get(route).json()["messages"]
    assert len(first) == 500
    assert first[0]["text"] == "direct-16"
    assert first[-12:] == api.get(route).json()["messages"][-12:]
    assert [item["text"] for item in first[-12:]] == [f"tied-{index}" for index in range(12)]
    assert [item["created_at"] for item in first] == sorted(item["created_at"] for item in first)


def test_transcript_includes_text_notes_but_not_status_only_events(unified_api):
    api = unified_api
    member = api.get("/api/state").json()
    team, sender = member["team"]["id"], member["me"]["id"]
    for text in ("Original prompt", "Revised prompt"):
        assert api.post("/api/prompt/edit", {"section": "Idea", "text": text}).status == 200
    assert api.post("/api/prompt/submit", {}).status == 200
    assert api.post("/api/prompt/vote", {"approve": True, "comment": "Prompt approved with comment"}, "b").status == 200
    recipient = api.get("/api/state", "b").json()["me"]["id"]
    with sqlite3.connect(api.config_dir / "hub.db") as database:
        database.executemany(
            "INSERT INTO events (team_id,member_id,kind,data,created_at) VALUES (?,?,?,?,?)",
            [(team, sender, "task_updated", json.dumps({"key": "T1", "note": "Task progress note"}), 1),
             (team, sender, "task_started", json.dumps({"key": "T1", "branch": "work"}), 2)],
        )
        database.execute(
            "INSERT INTO asks (team_id,id,from_id,to_id,question,answer,created_at,answered_at) VALUES (?,'Q7',?,?,'Restored question','Restored answer',3,3)",
            (team, sender, recipient),
        )
    messages = api.get(f"/api/legacy/transcript?team={team}").json()["messages"]
    by_text = {item["text"]: item for item in messages}
    assert set(by_text) == {"Original prompt", "Revised prompt", "Prompt approved with comment",
                            "Task progress note", "Restored question", "Restored answer"}
    assert by_text["Original prompt"]["kind"] == by_text["Revised prompt"]["kind"] == "master-prompt"
    assert by_text["Original prompt"]["prompt_version"] == 1
    assert by_text["Revised prompt"]["prompt_version"] == 2
    assert by_text["Task progress note"]["kind"] == "task note"
    assert by_text["Task progress note"]["task_id"] == "T1"
    assert by_text["Restored answer"]["kind"] == "answer"
    assert by_text["Restored answer"]["ask_id"] == "Q7"
    assert [item["text"] for item in messages if item.get("ask_id") == "Q7"] == [
        "Restored question", "Restored answer",
    ]


def test_transcript_keeps_newest_direct_messages_when_timestamps_tie(unified_api):
    api = unified_api
    team = api.get("/api/state").json()["team"]["id"]
    with api.server.state.condition:
        api.server.state.db.executemany(
            "INSERT INTO messages (id,sender,recipient,text,created_at) VALUES (?,'agent-a','agent-b',?,1)",
            [(str(uuid.uuid4()), f"direct-{index}") for index in range(501)],
        )
    messages = api.get(f"/api/legacy/transcript?team={team}").json()["messages"]
    assert [item["text"] for item in messages] == [f"direct-{index}" for index in range(1, 501)]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/state"), ("GET", "/api/members"),
    ("POST", "/api/messages"), ("POST", "/api/tasks"),
    ("POST", "/api/claims"), ("POST", "/api/questions"),
])
def test_removed_viewer_key_cannot_authenticate_as_a_team_member(unified_api, method, path):
    response = unified_api.request(method, path, "viewer", data={},
                                   headers={"X-Agentmesh-Agent": "agent-a"})
    assert response.status == 401
