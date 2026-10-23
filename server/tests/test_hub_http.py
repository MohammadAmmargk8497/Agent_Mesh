"""Loopback-only integration checks for the vendored hub's Python boundary."""

import http.client
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from agentmesh_server import hub
from agentmesh_server.config import init_config
from test_hub_seed import running_hub


@pytest.fixture
def hub_api(tmp_path):
    directory = tmp_path / "hub-config"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        api.config_dir = directory
        yield api


def test_create_join_invite_remove_leave_delete(hub_api):
    api = hub_api
    created = api.post("/api/teams", {"team_name": "Our team", "member_name": "Owner"}, None)
    assert created.status == 200, created.body
    team = created.json()
    owner = team["token"]
    assert team["dashboard_url"] == f"https://hub.example.invalid/t/{team['team_id']}#k={owner}"
    assert team["invite_url"].startswith(f"https://hub.example.invalid/join/{team['team_id']}.")
    joined = api.post("/api/join", {"code": team["invite_url"], "member_name": "Peer"}, None)
    assert joined.status == 200, joined.body
    peer = joined.json()["token"]
    assert api.post("/api/invite", {"rotate": True}, peer).status == 403
    assert api.post("/api/members/remove", {"member": "Owner"}, peer).status == 403
    assert api.post("/api/team/delete", {}, peer).status == 403
    assert api.post("/api/join", {"code": team["invite_url"], "member_name": "Extra"}, None).status == 200
    rotated = api.post("/api/invite", {"rotate": True}, owner)
    assert rotated.status == 200
    assert rotated.json()["invite_url"] != team["invite_url"]
    assert api.post("/api/join", {"code": team["invite_url"], "member_name": "Stale"}, None).status == 403
    assert api.post("/api/members/remove", {"member": "Peer"}, owner).json() == {"ok": True}
    assert api.get("/api/state", peer).status == 401
    extra = api.post("/api/join", {"code": rotated.json()["invite_url"], "member_name": "Last"}, None).json()["token"]
    assert api.post("/api/leave", {}, extra).json() == {"ok": True}
    assert api.get("/api/state", extra).status == 401
    assert api.post("/api/team/delete", {}, owner).json() == {"ok": True}
    assert api.get("/api/state", owner).status == 401
    assert api.get("/v1/whoami").status == 200


@pytest.mark.parametrize("disabled", [True, False])
def test_hub_unavailable_keeps_legacy_api(tmp_path, monkeypatch, disabled):
    directory = tmp_path / "unavailable"
    config = init_config(directory)
    if not disabled:
        monkeypatch.setattr(hub, "find_node", lambda: None)
    with running_hub(directory, config, hub=not disabled) as api:
        for path in ("/api", "/api/state", "/t/test", "/join/test.code"):
            response = api.get(path, None)
            assert response.status == 503
            assert response.json() == {"error": "hub unavailable"}
        assert api.get("/v1/whoami").json() == {"agent": "agent-a"}
        assert api.server.hub.process is None


def test_dead_child_keeps_legacy_api(hub_api):
    assert hub_api.get("/api/state").status == 200
    child = hub_api.server.hub.process
    child.kill()
    child.wait(timeout=3)
    assert hub_api.get("/api/state").status == 503
    assert hub_api.get("/t/test", None).status == 503
    assert hub_api.get("/v1/whoami").status == 200


def test_pages_local_nonce_escaped_inlined_and_not_proxied(hub_api):
    hub_api.server.hub.explicit_public_url = 'https://hub.example.invalid/?x="&y=<safe>'
    for path in ("/t/test", "/join/test.code"):
        response = hub_api.get(path, None)
        assert response.status == 200
        text = response.body.decode()
        csp = response.headers["content-security-policy"]
        nonce = csp.split("'nonce-", 1)[1].split("'", 1)[0]
        assert f'nonce="{nonce}"' in text
        assert "__AGENTMESH_NONCE__" not in text
        assert "{{HUB_URL}}" not in text and "{{INVITE_URL}}" not in text
        if path.startswith("/join/"):
            assert '?x=&quot;&amp;y=&lt;safe&gt;' in text
        assert 'src="/static/' not in text
        assert 'href="/static/' not in text
        assert "innerHTML" not in text
        assert response.headers["referrer-policy"] == "no-referrer"
    assert hub_api.server.hub.process is None
    robots = hub_api.get("/robots.txt", None)
    assert robots.status == 200 and b"Disallow: /t/" in robots.body


def test_proxy_replaces_forwarded_for_with_actual_peer(hub_api, monkeypatch):
    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(self.headers.get_all("X-Forwarded-For")).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    echo = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=echo.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setattr(hub_api.server.hub, "endpoint", lambda: urlsplit(f"http://127.0.0.1:{echo.server_port}"))
    try:
        response = hub_api.get("/api/state", headers={"X-Forwarded-For": "203.0.113.5, 198.51.100.2"})
        assert response.json() == ["127.0.0.1"]
    finally:
        echo.shutdown()
        echo.server_close()
        thread.join(timeout=3)


def test_sse_flushes_connected_and_events_without_buffering(hub_api):
    api = hub_api
    assert api.get("/api/state").status == 200
    connection = http.client.HTTPConnection(api.host, api.port, timeout=3)
    try:
        before = time.monotonic()
        connection.request("GET", "/api/stream", headers={"Authorization": "Bearer " + api.tokens["a"]})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        assert response.readline() == b": connected\n"
        assert time.monotonic() - before < 3
        assert api.post("/api/post", {"kind": "update", "text": "stream-test"}).status == 200
        for _ in range(40):
            if b"stream-test" in response.readline():
                break
        else:
            raise AssertionError("posted event did not stream")
    finally:
        connection.close()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_hub_database_symlink_refused_before_pages_or_api_start(hub_api, caplog, suffix):
    destination = hub_api.config_dir / "untouched"
    destination.write_text("preserve")
    (hub_api.config_dir / ("hub.db" + suffix)).symlink_to(destination)
    for path in ("/t/test", "/join/test.code", "/api/state"):
        result = hub_api.get(path)
        assert result.status == 503
        assert result.json() == {"error": "hub unavailable"}
        assert hub_api.server.hub.process is None
    assert destination.read_text() == "preserve"
    assert hub_api.get("/v1/whoami").status == 200
    messages = [record.getMessage() for record in caplog.records if record.name == "agentmesh_server.hub"]
    assert len(messages) == 1
    assert "symbolic links are refused" in messages[0]


def test_public_url_order_and_read_only_tailscale(monkeypatch):
    monkeypatch.setenv("AGENTMESH_PUBLIC_URL", "https://env.example/")
    assert hub.resolve_public_url("https://explicit.example/", "127.0.0.1", 1) == "https://explicit.example"
    assert hub.resolve_public_url(None, "127.0.0.1", 1) == "https://env.example"
    monkeypatch.delenv("AGENTMESH_PUBLIC_URL")
    monkeypatch.setattr(hub.shutil, "which", lambda name: "/not-executed/tailscale")
    calls = []

    def status(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b'{"Self":{"DNSName":"mesh.tail.example."}}')

    monkeypatch.setattr(hub.subprocess, "run", status)
    assert hub.resolve_public_url(None, "127.0.0.1", 1) == "https://mesh.tail.example"
    assert calls == [["/not-executed/tailscale", "status", "--json"]]
    monkeypatch.setattr(hub.shutil, "which", lambda name: None)
    assert hub.resolve_public_url(None, "::1", 123) == "http://[::1]:123"


def test_node_lookup_override_path_and_newest_nvm(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMESH_NODE", "/explicit/node")
    assert hub.find_node() == "/explicit/node"
    monkeypatch.delenv("AGENTMESH_NODE")
    monkeypatch.setattr(hub.shutil, "which", lambda name: "/path/node")
    assert hub.find_node() == "/path/node"
    monkeypatch.setattr(hub.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for version in ("v9.0.0", "v22.13.0", "v22.23.2"):
        node = tmp_path / ".nvm" / "versions" / "node" / version / "bin" / "node"
        node.parent.mkdir(parents=True)
        node.touch()
    assert hub.find_node().endswith("/v22.23.2/bin/node")


def test_child_environment_contains_no_credentials(monkeypatch):
    monkeypatch.setenv("AGENT_A_TOKEN", "fixture-only-" + "a" * 40)
    monkeypatch.setenv("AGENTMESH_CF_ACCESS_CLIENT_SECRET", "fixture-only-" + "s" * 40)
    monkeypatch.setenv("NODE_OPTIONS", "--not-inherited")
    assert not {"AGENT_A_TOKEN", "AGENTMESH_CF_ACCESS_CLIENT_SECRET", "NODE_OPTIONS"} & hub.HubSupervisor._child_env().keys()
