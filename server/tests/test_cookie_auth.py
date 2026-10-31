"""Local-only browser session and CSRF checks; no external service is contacted."""

import http.client
import json
import threading
from email.message import Message
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.request import Request
from urllib.parse import urlsplit

import pytest

from agentmesh_server.config import init_config
from agentmesh_server.http import Handler, HttpError
from agentmesh_server.hub import HubUnavailable
from conftest import Response
from test_hub_seed import running_hub


@pytest.fixture(scope="module")
def browser_api(tmp_path_factory):
    directory = tmp_path_factory.mktemp("browser-session")
    config = init_config(directory)
    with running_hub(directory, config) as api:
        yield api


def origin(api):
    return f"http://{api.host}:{api.port}"


def login(api, role="a", **kwargs):
    return api.post("/auth/login", {"key": api.tokens.get(role, role)}, None,
                    headers={"Origin": origin(api)}, **kwargs)


def cookie(api, role="a"):
    response = login(api, role)
    assert response.status == 200, response.body
    return response.headers["set-cookie"].split(";", 1)[0]


def assert_expired_cookie(response):
    session = SimpleCookie(response.headers["set-cookie"])["agentmesh_session"]
    assert session.value == ""
    assert session["max-age"] == "0"
    assert session["httponly"] and session["secure"]
    assert session["samesite"] == "Strict" and session["path"] == "/"
    assert not session["domain"]


def raw_request(api, path, headers):
    """Preserve duplicate headers so origin/host ambiguity is exercised."""
    connection = http.client.HTTPConnection(api.host, api.port, timeout=5)
    try:
        connection.putrequest("POST", path, skip_host=True)
        connection.putheader("Content-Length", "2")
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(b"{}")
        result = connection.getresponse()
        return Response(result.status, result.read(),
                        {name.lower(): value for name, value in result.getheaders()})
    finally:
        connection.close()


def test_login_returns_team_only_and_persistent_httponly_cookie(browser_api):
    api = browser_api
    expected_team = api.get("/api/state").json()["team"]["id"]
    response = login(api)
    assert response.status == 200, response.body
    assert response.json() == {"team_id": expected_team}
    assert response.headers["cache-control"] == "no-store"
    session = SimpleCookie(response.headers["set-cookie"])["agentmesh_session"]
    assert session.value == api.tokens["a"]
    assert session["httponly"] and session["secure"]
    assert session["samesite"] == "Strict"
    assert session["path"] == "/"
    assert session["max-age"] == "2592000"
    assert not session["domain"]
    assert api.tokens["a"].encode() not in response.body
    page = api.get("/", None, headers={"Cookie": cookie(api)})
    assert page.status == 200
    assert api.tokens["a"].encode() not in page.body
    assert "nonce-" in page.headers["content-security-policy"]


@pytest.mark.parametrize("key", ["not-a-real-key", "", None, 17, "invalid\r\nInjected: yes", "x; y=z"])
def test_login_rejects_bad_keys_and_expires_cookie_even_with_valid_bearer(browser_api, key):
    api = browser_api
    result = api.post("/auth/login", {"key": key},
                      headers={"Origin": origin(api)})
    assert result.status == 401
    assert_expired_cookie(result)


def test_rejected_login_removes_previous_valid_session_from_cookie_jar(browser_api):
    api = browser_api
    jar = CookieJar()
    browser_origin = f"https://{api.host}:{api.port}"

    def receive(result):
        headers = Message()
        headers["Set-Cookie"] = result.headers["set-cookie"]
        jar.extract_cookies(SimpleNamespace(info=lambda: headers),
                            Request(browser_origin + "/auth/login"))

    receive(login(api))
    assert len(jar) == 1
    authenticated_request = Request(browser_origin + "/api/state")
    jar.add_cookie_header(authenticated_request)
    session = authenticated_request.get_header("Cookie")
    assert api.get("/api/state", None, headers={"Cookie": session}).status == 200
    rejected = api.post("/auth/login", {"key": "rejected-fixture-key"}, None,
                        headers={"Cookie": session, "Origin": origin(api)})
    assert rejected.status == 401
    assert_expired_cookie(rejected)
    receive(rejected)
    assert len(jar) == 0
    reload_request = Request(browser_origin + "/api/state")
    jar.add_cookie_header(reload_request)
    assert reload_request.get_header("Cookie") is None
    assert api.get("/api/state", None).status == 401


@pytest.mark.parametrize("status", [401, 403])
def test_member_rejection_normalizes_unauthorized_and_expires_cookie(browser_api, monkeypatch, status):
    def rejected_member(self, key):
        raise HttpError(status, "member rejected")

    monkeypatch.setattr(Handler, "_member_state", rejected_member)
    response = login(browser_api)
    assert response.status == 401
    assert_expired_cookie(response)


def test_unavailable_hub_does_not_clear_previous_cookie(browser_api, monkeypatch):
    def unavailable_member(self, key):
        raise HubUnavailable("hub unavailable")

    monkeypatch.setattr(Handler, "_member_state", unavailable_member)
    response = login(browser_api)
    assert response.status == 503
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("path", ["/auth/login", "/auth/logout", "/auth/unknown"])
@pytest.mark.parametrize("bad_origin", [None, "null", "https://evil.example", "suffix", "port",
                                        "path", "userinfo", "fragment", "query", "list"])
def test_auth_posts_require_exact_same_origin(browser_api, path, bad_origin):
    api = browser_api
    replacements = {
        "suffix": origin(api) + ".evil.example",
        "port": f"http://{api.host}:{api.port + 1}",
        "path": origin(api) + "/",
        "userinfo": f"http://evil@{api.host}:{api.port}",
        "fragment": origin(api) + "#",
        "query": origin(api) + "?",
        "list": origin(api) + " https://evil.example",
    }
    headers = {} if bad_origin is None else {"Origin": replacements.get(bad_origin, bad_origin)}
    response = api.post(path, {"key": api.tokens["a"]}, None, headers=headers)
    assert response.status == 403, response.body
    assert "set-cookie" not in response.headers


def test_same_host_https_origin_supports_tls_termination(browser_api):
    api = browser_api
    headers = {"Origin": f"https://{api.host}:{api.port}", "X-Forwarded-Proto": "https"}
    response = api.post("/auth/login", {"key": api.tokens["a"]}, None,
                        headers=headers)
    assert response.status == 200
    headers["Origin"] = origin(api)
    response = api.post("/auth/login", {"key": api.tokens["a"]}, None, headers=headers)
    assert response.status == 403
    assert "set-cookie" not in response.headers
    response = api.post("/auth/login", {"key": api.tokens["a"]}, None,
                        headers={"Origin": f"https://{api.host}:{api.port}"})
    assert response.status == 403
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("forwarded", ["", "HTTPS", "ftp", "https, http", "https/", "null"])
def test_invalid_trusted_forwarded_protocol_is_rejected(browser_api, forwarded):
    api = browser_api
    result = api.post("/auth/logout", {}, None,
                      headers={"Origin": origin(api), "X-Forwarded-Proto": forwarded})
    assert result.status == 403
    assert "set-cookie" not in result.headers


def test_duplicate_trusted_forwarded_protocol_is_rejected(browser_api):
    api = browser_api
    result = raw_request(api, "/auth/logout", [
        ("Host", f"{api.host}:{api.port}"), ("Origin", origin(api)),
        ("X-Forwarded-Proto", "http"), ("X-Forwarded-Proto", "http"),
    ])
    assert result.status == 403
    assert "set-cookie" not in result.headers


@pytest.mark.parametrize("forwarded", [["https"], ["https", "http"], ["invalid"]])
def test_remote_forwarded_protocol_is_ignored(forwarded):
    handler = object.__new__(Handler)
    handler.connection = object()
    handler.client_address = ("198.51.100.24", 12345)
    handler.headers = Message()
    handler.headers["Host"] = "mesh.example:8787"
    handler.headers["Origin"] = "http://mesh.example:8787"
    for value in forwarded:
        handler.headers["X-Forwarded-Proto"] = value
    handler._require_same_origin()
    handler.headers.replace_header("Origin", "https://mesh.example:8787")
    with pytest.raises(HttpError) as error:
        handler._require_same_origin()
    assert error.value.status == 403


def test_proxy_strips_cookies_but_preserves_synthesized_and_explicit_authorization(browser_api, monkeypatch):
    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"cookie": self.headers.get_all("Cookie", []),
                               "authorization": self.headers.get_all("Authorization", [])}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    api = browser_api
    echo = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=echo.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setattr(api.server.hub, "endpoint", lambda: urlsplit(f"http://127.0.0.1:{echo.server_port}"))
    headers = {"Cookie": "agentmesh_session=" + api.tokens["a"] + "; unrelated=private"}
    try:
        result = api.get("/api/state", None, headers=headers)
        assert result.json() == {"cookie": [], "authorization": ["Bearer " + api.tokens["a"]]}
        result = api.get("/api/state", "b", headers=headers)
        assert result.json() == {"cookie": [], "authorization": ["Bearer " + api.tokens["b"]]}
        result = api.get("/api/state", None, headers={**headers, "Authorization": ""})
        assert result.json() == {"cookie": [], "authorization": [""]}
    finally:
        echo.shutdown()
        echo.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("duplicates", ["Origin", "Host"])
@pytest.mark.parametrize("path", ["/auth/logout", "/api/post", "/v1/messages"])
def test_duplicate_origin_or_host_is_rejected(browser_api, duplicates, path):
    api = browser_api
    headers = [("Host", f"{api.host}:{api.port}"), ("Origin", origin(api)),
               ("Cookie", cookie(api))]
    headers.append((duplicates, origin(api) if duplicates == "Origin" else f"{api.host}:{api.port}"))
    assert raw_request(api, path, headers).status == 403


def test_cookie_only_reads_cover_hub_native_and_legacy_adapter(browser_api):
    api = browser_api
    session = cookie(api)
    headers = {"Cookie": "unrelated=present; " + session}
    state = api.get("/api/state", None, headers=headers)
    assert state.status == 200, state.body
    assert state.json()["me"]["name"] == "agent-a"
    assert api.get("/v1/whoami", None, headers=headers).json() == {"agent": "agent-a"}
    team = state.json()["team"]["id"]
    assert api.get("/api/legacy/transcript?team=" + team, None, headers=headers).status == 200
    # A fresh profile has neither the caller's cookie nor any server-side ambient identity.
    assert api.get("/api/state", None).status == 401
    # Reusing the persistent cookie after a simulated page reload still works.
    assert api.get("/api/state", None, headers=headers).status == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("path", ["/api/post", "/v1/messages"])
@pytest.mark.parametrize("with_origin", [False, True])
def test_cookie_mutations_require_same_origin(browser_api, method, path, with_origin):
    api = browser_api
    headers = {"Cookie": "agentmesh_session=" + api.tokens["a"]}
    if with_origin:
        headers["Origin"] = "https://evil.example"
    response = api.request(method, path, None, {}, headers=headers)
    assert response.status == 403, response.body


def test_cookie_only_same_origin_writes_and_bearer_clients_work(browser_api):
    api = browser_api
    headers = {"Cookie": cookie(api), "Origin": origin(api)}
    result = api.post("/api/post", {"kind": "update", "text": "cookie-hub-write"}, None,
                      headers=headers)
    assert result.status == 200, result.body
    result = api.post("/v1/messages", {"to": "agent-b", "text": "cookie-native-write"}, None,
                      headers=headers)
    assert result.status == 201, result.body
    # Non-browser API clients keep working without an Origin header.
    assert api.post("/api/post", {"kind": "update", "text": "bearer-hub-write"}).status == 200
    assert api.post("/v1/messages", {"to": "agent-b", "text": "bearer-native-write"}).status == 201


@pytest.mark.parametrize("path", ["/api/state", "/v1/whoami"])
@pytest.mark.parametrize("request_origin", [None, "https://evil.example"])
def test_cookie_websocket_upgrade_requires_same_origin(browser_api, path, request_origin):
    api = browser_api
    headers = {"Cookie": "agentmesh_session=" + api.tokens["a"], "Upgrade": "websocket"}
    if request_origin is not None:
        headers["Origin"] = request_origin
    result = api.get(path, None, headers=headers)
    assert result.status == 403
    assert "set-cookie" not in result.headers


@pytest.mark.parametrize("path", ["/api/state", "/v1/whoami"])
def test_same_origin_cookie_websocket_upgrade_preserves_existing_get_route(browser_api, path):
    api = browser_api
    headers = {"Cookie": "agentmesh_session=" + api.tokens["a"], "Upgrade": "WebSocket",
               "Origin": origin(api)}
    assert api.get(path, None, headers=headers).status == 200


@pytest.mark.parametrize("path", ["/api/state", "/v1/whoami"])
@pytest.mark.parametrize("request_origin", [None, "https://evil.example"])
def test_explicit_bearer_websocket_upgrade_keeps_existing_origin_behavior(browser_api, path, request_origin):
    api = browser_api
    headers = {"Cookie": "agentmesh_session=" + api.tokens["a"], "Upgrade": "websocket"}
    if request_origin is not None:
        headers["Origin"] = request_origin
    result = api.get(path, "b", headers=headers)
    assert result.status == 200
    assert "set-cookie" not in result.headers


@pytest.mark.parametrize("path", ["/api/state", "/v1/whoami"])
@pytest.mark.parametrize("header", ["", "Basic invalid", "Bearer invalid"])
def test_explicit_invalid_or_empty_authorization_never_uses_cookie(browser_api, path, header):
    api = browser_api
    result = api.get(path, None, headers={"Cookie": cookie(api), "Authorization": header})
    assert result.status == 401, result.body
    assert "set-cookie" not in result.headers


def test_explicit_bearer_takes_precedence_and_does_not_add_csrf_requirement(browser_api):
    api = browser_api
    headers = {"Cookie": cookie(api), "Origin": "https://evil.example"}
    state = api.get("/api/state", "b", headers=headers)
    assert state.status == 200
    assert state.json()["me"]["name"] == "agent-b"
    assert api.get("/v1/whoami", "b", headers=headers).json() == {"agent": "agent-b"}
    assert api.post("/api/post", {"kind": "update", "text": "explicit bearer"}, "b",
                    headers=headers).status == 200
    assert api.post("/v1/messages", {"to": "agent-a", "text": "explicit bearer"}, "b",
                    headers=headers).status == 201


@pytest.mark.parametrize("bad_cookie", ["duplicate", "malformed", "unknown"])
@pytest.mark.parametrize("path", ["/api/state", "/v1/whoami"])
def test_invalid_or_ambiguous_session_cookie_is_not_authenticated(browser_api, bad_cookie, path):
    api = browser_api
    values = {
        "duplicate": "agentmesh_session=" + api.tokens["a"] + "; agentmesh_session=" + api.tokens["b"],
        "malformed": 'agentmesh_session="unterminated',
        "unknown": "agentmesh_session=not-a-real-key",
    }
    result = api.get(path, None, headers={"Cookie": values[bad_cookie]})
    assert result.status == 401, result.body


def test_logout_clears_the_persistent_cookie(browser_api):
    api = browser_api
    result = api.post("/auth/logout", {}, None,
                      headers={"Cookie": cookie(api), "Origin": origin(api)})
    assert result.status == 200
    assert result.json() == {"ok": True}
    assert_expired_cookie(result)
    assert api.get("/api/state", None, headers={"Cookie": "agentmesh_session="}).status == 401


def test_cookie_authentication_supports_sse(browser_api):
    api = browser_api
    session = cookie(api)
    connection = http.client.HTTPConnection(api.host, api.port, timeout=3)
    response = None
    try:
        connection.request("GET", "/api/stream", headers={"Cookie": session})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        assert response.readline() == b": connected\n"
        result = api.post("/api/post", {"kind": "update", "text": "cookie-stream-event"}, None,
                          headers={"Cookie": session, "Origin": origin(api)})
        assert result.status == 200
        for _ in range(40):
            if b"cookie-stream-event" in response.readline():
                break
        else:
            raise AssertionError("cookie-authenticated SSE event did not arrive")
    finally:
        if response is not None:
            response.close()
        connection.close()


def test_new_team_member_uses_cookie_without_acquiring_legacy_agent_permissions(browser_api):
    api = browser_api
    team = api.post("/api/teams", {"team_name": "Cookie team", "member_name": "Owner"}, None).json()
    result = login(api, team["token"])
    assert result.status == 200, result.body
    assert result.json() == {"team_id": team["team_id"]}
    session = result.headers["set-cookie"].split(";", 1)[0]
    assert api.get("/api/state", None, headers={"Cookie": session}).json()["team"]["id"] == team["team_id"]
    assert api.get("/v1/whoami", None, headers={"Cookie": session}).status == 401
    # Removing the member invalidates the cookie via normal hub authorization.
    assert api.post("/api/team/delete", {}, team["token"]).status == 200
    assert api.get("/api/state", None, headers={"Cookie": session}).status == 401


def test_rotated_legacy_key_is_not_a_valid_browser_session(tmp_path):
    directory = tmp_path / "rotated-session"
    config = init_config(directory)
    old_key = config["AGENT_A_TOKEN"]
    config["AGENT_A_TOKEN"] = "new-current-fixture-token-" + "x" * 32 + "," + old_key
    (directory / "config").write_text(json.dumps(config))
    with running_hub(directory, config) as api:
        assert api.get("/v1/whoami", old_key).status == 200
        assert api.get("/api/state", old_key).status == 401
        session = "agentmesh_session=" + old_key
        assert api.get("/v1/whoami", None, headers={"Cookie": session}).status == 401
        assert login(api, old_key).status == 401
