"""Unified page delivery and member-key isolation for legacy read adapters."""

import re
import sqlite3
from importlib.resources import files

import pytest

from agentmesh_server.config import init_config
from test_hub_seed import running_hub


@pytest.fixture
def unified_api(tmp_path):
    directory = tmp_path / "unified"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        api.config_dir = directory
        yield api


def test_same_dashboard_at_team_root_and_legacy_viewer_signin_preserved(unified_api):
    api = unified_api
    for path, name in (("/", "dashboard.html"), ("/viewer", "dashboard.html"),
                       ("/t/texample", "dashboard.html"), ("/index.html", "signin.html")):
        result = api.get(path, None)
        assert result.status == 200
        nonce = re.search(r"script-src 'nonce-([^']+)'", result.headers["content-security-policy"])[1]
        template = files("agentmesh_server").joinpath(name).read_text(encoding="utf-8")
        text = result.body.decode()
        assert text == template.replace("__AGENTMESH_NONCE__", nonce)
        assert not re.search("openhuddle", text, re.I)
        assert all(token not in text for token in api.tokens.values())
        head = api.request("HEAD", path, None)
        assert head.status == 200
        assert head.body == b""
    assert api.server.hub.process is None, "page delivery must not start the hub subprocess"


def test_default_team_generated_key_reads_legacy_snapshot_and_patch(unified_api):
    api = unified_api
    state = api.get("/api/state").json()
    team = state["team"]["id"]
    invite = api.post("/api/invite", {}).json()["invite_url"]
    joined = api.post("/api/join", {"code": invite, "member_name": "Observer"}, None).json()
    message = api.post("/v1/messages", {"to": "agent-b", "text": "default-team-only"}).json()
    patch = api.post("/v1/patches", {"title": "Private patch", "text": "private diff"}).json()
    # Resolving the seeded identity must not fake agent-a's presence, even for a
    # generated member's first adapter request.
    with sqlite3.connect(api.config_dir / "hub.db") as database:
        database.execute("UPDATE members SET last_seen=1 WHERE id=?", (state["me"]["id"],))
    route = f"/api/legacy/transcript?team={team}"
    result = api.get(route, joined["token"])
    assert result.status == 200, result.body
    snapshot = result.json()
    assert any(row["id"] == message["id"] for row in snapshot["messages"])
    assert any(row["id"] == patch["id"] for row in snapshot["patches"])
    assert snapshot["audit"]
    assert api.get(f"/api/legacy/patches?team={team}", joined["token"]).json()["patches"][0]["id"] == patch["id"]
    assert api.get(f"/api/legacy/patches/{patch['id']}?team={team}", joined["token"]).json()["patch"]["text"] == "private diff"
    with sqlite3.connect(api.config_dir / "hub.db") as database:
        assert database.execute("SELECT last_seen FROM members WHERE id=?", (state["me"]["id"],)).fetchone()[0] == 1
    assert api.get(route + "&after=not-an-integer", joined["token"]).status == 400
    assert api.get("/v1/transcript", "a").status == 403
    assert api.get("/v1/transcript", "viewer").status == 200
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
