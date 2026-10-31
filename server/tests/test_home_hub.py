"""The home front door uses local SQLite only, including after a restart."""

import ipaddress
import socket
import sqlite3
import threading
from contextlib import contextmanager

from agentmesh_server.config import init_config
from agentmesh_server.http import create_server
from conftest import LocalAPI


@contextmanager
def home_server(directory, config):
    server = create_server(directory, port=0,
                           public_url="https://agentmesh-home.zahid.win")
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield LocalAPI(server, config)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_home_health_dashboard_and_both_apis_persist_without_remote_calls(tmp_path, monkeypatch):
    connect = socket.socket.connect

    def loopback_only(sock, address):
        # Reject accidental use of any remote API by the Python front door.
        assert ipaddress.ip_address(address[0]).is_loopback, address
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    directory = tmp_path / "home"
    config = init_config(directory)
    with home_server(directory, config) as api:
        assert api.host == "127.0.0.1"
        assert api.get("/health", None).json() == {"ok": True, "service": "agentmesh"}
        dashboard = api.get("/", None)
        assert dashboard.status == 200
        assert dashboard.headers["content-type"].startswith("text/html")
        assert b"AgentMesh" in dashboard.body
        assert "nonce-" in dashboard.headers["content-security-policy"]
        assert api.server.hub.process is None

        state = api.get("/api/state")
        assert state.status == 200, state.body
        team = state.json()["team"]["id"]
        assert state.json()["me"]["name"] == "agent-a"
        invite = api.post("/api/invite", {})
        assert invite.status == 200, invite.body
        assert invite.json()["invite_url"].startswith("https://agentmesh-home.zahid.win/join/")
        assert api.get("/t/" + team, None).status == 200
        assert api.post("/api/post", {"kind": "update", "text": "home hub persists"}).status == 200
        legacy = api.post("/v1/messages", {"to": "agent-b", "text": "home legacy persists"})
        assert legacy.status == 201, legacy.body
        assert api.server.hub.process is not None
        assert api.server.hub.endpoint().hostname == "127.0.0.1"

    for name in ("data.db", "hub.db"):
        assert (directory / name).stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(directory / "data.db") as database:
        assert database.execute("SELECT text FROM messages").fetchone() == ("home legacy persists",)
    with sqlite3.connect(directory / "hub.db") as database:
        assert database.execute("SELECT count(*) FROM events WHERE data LIKE ?",
                                ("%home hub persists%",)).fetchone() == (1,)

    with home_server(directory, config) as api:
        assert api.get("/health", None).status == 200
        assert api.get("/", None).status == 200
        assert api.get("/api/state").json()["team"]["id"] == team
        news = api.get("/api/events?since=0")
        assert news.status == 200, news.body
        assert b"home hub persists" in news.body
        inbox = api.get("/v1/inbox", "b").json()
        assert [message["text"] for message in inbox["unread_messages"]] == ["home legacy persists"]
