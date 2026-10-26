"""Unified page delivery and member-key isolation for legacy read adapters."""

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
        assert api.get(f"/api/legacy{suffix}?team={other['team_id']}", other["token"]).status == 403
        assert api.get(f"/api/legacy{suffix}?team={team}", other["token"],
                       headers={"X-Agentmesh-Agent": "viewer"}).status == 403
        assert api.get(f"/api/legacy{suffix}?team={other['team_id']}").status == 403
    route = f"/api/legacy/transcript?team={team}"
    assert api.get(route, None).status == 401
    assert api.get(route, "viewer").status == 401
    assert api.get("/api/legacy/transcript").status == 403
    assert api.post(route, {}).status == 405
    assert api.get(f"/api/legacy/tasks?team={team}").status == 404


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/state"), ("GET", "/api/members"),
    ("POST", "/api/messages"), ("POST", "/api/tasks"),
    ("POST", "/api/claims"), ("POST", "/api/questions"),
])
def test_removed_viewer_key_cannot_authenticate_as_a_team_member(unified_api, method, path):
    response = unified_api.request(method, path, "viewer", data={},
                                   headers={"X-Agentmesh-Agent": "agent-a"})
    assert response.status == 401
