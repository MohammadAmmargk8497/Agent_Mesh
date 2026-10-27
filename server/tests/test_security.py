import re
from pathlib import Path

import pytest


def test_dashboard_package_matches_worker_byte_for_byte():
    from importlib.resources import files

    expected = Path(__file__).resolve().parents[2] / "cloudflare" / "src" / "dashboard.html"
    assert files("agentmesh_server").joinpath("dashboard.html").read_bytes() == expected.read_bytes()


def test_signin_package_matches_worker_byte_for_byte():
    from importlib.resources import files

    expected = Path(__file__).resolve().parents[2] / "cloudflare" / "src" / "signin.html"
    assert files("agentmesh_server").joinpath("signin.html").read_bytes() == expected.read_bytes()


@pytest.mark.parametrize("page,paths", [
    ("dashboard.html", ("/", "/", "/")),
])
def test_public_pages_fresh_nonce_csp_and_no_inline_tokens(api, page, paths):
    from importlib.resources import files

    template = files("agentmesh_server").joinpath(page).read_text(encoding="utf-8")
    nonces = []
    for path in paths:
        result = api.get(path, None)
        assert result.status == 200
        assert result.headers["content-type"] == "text/html; charset=utf-8"
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-content-type-options"] == "nosniff"
        html = result.body.decode()
        assert "__AGENTMESH_NONCE__" not in html
        matches = re.findall(r'nonce="([^"]+)"', html)
        assert matches and len(set(matches)) == 1
        nonce = matches[0]
        nonces.append(nonce)
        assert html == template.replace("__AGENTMESH_NONCE__", nonce)
        assert result.headers["content-security-policy"] == (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        assert all(token not in html for token in api.tokens.values())
    assert len(set(nonces)) == 3


@pytest.mark.parametrize("path", ["/viewer", "/index.html", "/viewer?after=1", "/index.html?team=default"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_obsolete_dashboard_pages_redirect_to_root(api, path, method):
    response = api.request(method, path, None)
    assert response.status == 302
    assert response.headers["location"] == "/"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.body == b""


def test_ratelimit_121st_unauthenticated_request_and_proxy_ip_isolation(api):
    headers = {"X-Forwarded-For": "100.64.1.1"}
    for _ in range(120):
        assert api.get("/v1/whoami", None, headers=headers).status == 401
    assert api.get("/v1/whoami", None, headers=headers).status == 429
    assert api.get("/v1/whoami", None, headers={"X-Forwarded-For": "100.64.1.2"}).status == 401
    assert api.get("/v1/whoami").status == 200


def test_ratelimit_socket_peer_and_forwarded_header_policy():
    from agentmesh_server.http import peer_ip

    assert peer_ip("100.64.0.5", "100.64.0.6") == "100.64.0.5"
    assert peer_ip("192.168.1.2", "100.64.0.6") == "192.168.1.2"
    assert peer_ip("127.0.0.1", "100.64.0.6") == "100.64.0.6"
    assert peer_ip("::1", "100.64.0.6, 127.0.0.1") == "127.0.0.1"
    assert peer_ip("127.0.0.1", "not-an-address") == "127.0.0.1"
    assert peer_ip("127.0.0.1", None) == "127.0.0.1"


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("127.0.0.1", "198.51.100.1, 100.64.0.6", "100.64.0.6"),
    ("::1", "198.51.100.1, 100.64.0.6, fd7a:115c:a1e0::2", "fd7a:115c:a1e0::2"),
    ("::ffff:127.0.0.1", "not-an-address, 100.64.0.6", "100.64.0.6"),
    ("100.64.0.5", "198.51.100.1, 100.64.0.6", "100.64.0.5"),
    ("127.0.0.1", "100.64.0.6, not-an-address", "127.0.0.1"),
    ("::1", "100.64.0.6, ", "::1"),
])
def test_ratelimit_forwarded_chain_uses_last_proxy_entry(peer, forwarded, expected):
    from agentmesh_server.http import peer_ip

    assert peer_ip(peer, forwarded) == expected


def test_ratelimit_proxy_appended_ip_prevents_spoofed_prefix_bypass(api):
    for index in range(120):
        headers = {"X-Forwarded-For": f"198.51.100.{index + 1}, 100.64.1.1"}
        assert api.get("/v1/whoami", None, headers=headers).status == 401
    headers = {"X-Forwarded-For": "203.0.113.1, 100.64.1.1"}
    assert api.get("/v1/whoami", None, headers=headers).status == 429
    headers = {"X-Forwarded-For": "203.0.113.1, 100.64.1.2"}
    assert api.get("/v1/whoami", None, headers=headers).status == 401


def test_ratelimit_repeated_forwarded_headers_use_last_proxy_entry(api):
    import http.client

    connection = http.client.HTTPConnection(api.host, api.port, timeout=3)
    try:
        connection.putrequest("GET", "/v1/whoami")
        connection.putheader("X-Forwarded-For", "198.51.100.1")
        connection.putheader("X-Forwarded-For", "203.0.113.1, 100.64.1.1")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 401
        response.read()
    finally:
        connection.close()
    assert set(api.server.state.limiter.buckets) == {"ip:100.64.1.1"}


def test_ratelimit_non_loopback_peer_never_reads_forwarded_header(api, monkeypatch):
    from http.client import HTTPMessage

    finish_request = api.server.finish_request
    get_header = HTTPMessage.get
    get_all_headers = HTTPMessage.get_all

    def remote_request(request, client_address):
        finish_request(request, ("100.64.0.5", client_address[1]))

    def guarded_header(headers, name, default=None):
        if name.lower() == "x-forwarded-for":
            raise AssertionError("non-loopback request must not read forwarded headers")
        return get_header(headers, name, default)

    def guarded_all_headers(headers, name, default=None):
        if name.lower() == "x-forwarded-for":
            raise AssertionError("non-loopback request must not read forwarded headers")
        return get_all_headers(headers, name, default)

    monkeypatch.setattr(api.server, "finish_request", remote_request)
    monkeypatch.setattr(HTTPMessage, "get", guarded_header)
    monkeypatch.setattr(HTTPMessage, "get_all", guarded_all_headers)
    result = api.get("/v1/whoami", None, headers={"X-Forwarded-For": "203.0.113.1"})
    assert result.status == 401
    assert set(api.server.state.limiter.buckets) == {"ip:100.64.0.5"}


def test_ratelimit_ignores_spoofed_identity(api):
    for _ in range(120):
        assert api.get("/v1/whoami", None, headers={"x-agentmesh-agent": "agent-a"}).status == 401
    assert api.get("/v1/whoami", None, headers={"x-agentmesh-agent": "agent-b"}).status == 429


def test_sweep_preserves_all_history_regardless_of_age_or_status(api):
    day = 86_400_000
    now = 200 * day
    state = api.server.state
    with state.lock:
        db = state.db
        for table in ("events", "audit"):
            if table == "events":
                for age in (7 * day + 1, 7 * day, 0):
                    db.execute("INSERT INTO events (recipient,type,ref,actor,created_at) VALUES (?,?,?,?,?)",
                               ("agent-b", "message", str(age), "agent-a", now - age))
            else:
                for age in (7 * day + 1, 7 * day, 0):
                    db.execute("INSERT INTO audit (actor,action,ref,created_at) VALUES (?,?,?,?)",
                               ("agent-a", "message.send", str(age), now - age))
        for name, age, acked in (("old-acked", 30 * day + 1, True),
                                 ("boundary-acked", 30 * day, True),
                                 ("old-unacked", 100 * day, False),
                                 ("new-acked", 0, True)):
            db.execute("INSERT INTO messages (id,sender,recipient,text,task_id,created_at,acked_at) VALUES (?,?,?,?,?,?,?)",
                       (name, "agent-a", "agent-b", name, None, now - 100 * day, now - age if acked else None))
        for name, age in (("old", 30 * day + 1), ("boundary", 30 * day), ("new", 0)):
            db.execute("INSERT INTO patches (id,sender,title,text,task_id,created_at) VALUES (?,?,?,?,?,?)",
                       (name, "agent-a", name, "text", None, now - age))
        for status in ("open", "in_progress", "done", "blocked", "accepted", "cancelled"):
            for age, suffix in ((90 * day + 1, "old"), (90 * day, "boundary")):
                db.execute("INSERT INTO tasks (id,title,description,assignee,created_by,status,acceptance_criteria,result,created_at,updated_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)", (status + suffix, "t", "d", "agent-b", "agent-a", status, "[]", None, now - age, now - age))
        db.commit()
        tables = ("events", "audit", "messages", "patches", "tasks")
        before = {table: state.rows(f"SELECT * FROM {table} ORDER BY rowid")
                  for table in tables}
    state.sweep(now=now)
    with state.lock:
        assert {table: state.rows(f"SELECT * FROM {table} ORDER BY rowid")
                for table in tables} == before
        for table in ("events", "audit"):
            assert [row[0] for row in state.db.execute(f"SELECT created_at FROM {table} ORDER BY created_at")] == [now - 7 * day - 1, now - 7 * day, now]
        assert {row[0] for row in state.db.execute("SELECT id FROM messages")} == {"old-acked", "boundary-acked", "old-unacked", "new-acked"}
        assert {row[0] for row in state.db.execute("SELECT id FROM patches")} == {"old", "boundary", "new"}
        tasks = {row[0] for row in state.db.execute("SELECT id FROM tasks")}
        assert "acceptedold" in tasks and "cancelledold" in tasks
        assert len(tasks) == 12
        assert {"acceptedboundary", "cancelledboundary", "openold", "in_progressold", "doneold", "blockedold"} <= tasks


def test_rotated_tokens_accept_new_and_old(tmp_path):
    import json
    import threading

    from agentmesh_server.config import init_config
    from agentmesh_server.http import create_server
    from conftest import LocalAPI

    config_dir = tmp_path / "rotated"
    config = init_config(config_dir)
    old_token = config["AGENT_A_TOKEN"]
    new_token = "new-agent-a-token-" + "x" * 32
    config["AGENT_A_TOKEN"] = new_token + "," + old_token
    (config_dir / "config").write_text(json.dumps(config))
    server = create_server(config_dir, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        api = LocalAPI(server, config)
        for token in (new_token, old_token):
            response = api.get("/v1/whoami", token)
            assert response.status == 200
            assert response.json() == {"agent": "agent-a"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_remote_bind_requires_explicit_flag(tmp_path):
    from agentmesh_server.config import init_config
    from agentmesh_server.http import create_server

    config_dir = tmp_path / "bind"
    init_config(config_dir)
    with pytest.raises((ValueError, RuntimeError), match="(?i)(loopback|remote|allow)"):
        create_server(config_dir, host="0.0.0.0", port=0)
    server = create_server(config_dir, host="0.0.0.0", port=0, allow_remote_bind=True)
    server.server_close()
