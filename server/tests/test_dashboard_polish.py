"""Real Chrome checks using a loopback-only copy of home dashboard data.

No live hub, credentials, dependencies, services or external requests are used.
Run: cd server && PYTHONPATH=src python3 -m pytest -q tests/test_dashboard_polish.py
"""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from agentmesh_server.dashboard_features import compose_dashboard
from agentmesh_server.hub import find_node
from team_features_e2e import chrome_temporary_directory


SERVER = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).with_name("dashboard_polish_chrome.cjs")
NONCE = "dashboard-local-fixture-nonce"
CREDENTIAL = "local-snapshot-credential-" + "x" * 40
MALICIOUS = '<img id="fixture-injection" src=x onerror="window.__injected=true">'


def home_snapshot():
    """Representative loaded home data, never a production export or API read."""
    now = datetime.now(timezone.utc)

    def stamp(minutes=0):
        return (now - timedelta(minutes=minutes)).isoformat()

    tasks = [
        {"key": "T-101", "title": "Aurora snapshot task", "status": "open"},
        {"key": "T-102", "title": "Current constellation work", "status": "in_progress", "assignee_id": "member-a"},
        {"key": "T-103", "title": "Finished telescope calibration", "status": "done", "assignee_id": "member-a"},
        {"key": "T-104", "title": "Blocked observatory release", "status": "assigned", "assignee_id": "member-b", "depends_on": ["T-101"], "ready": False},
        {"key": "T-105", "title": "Completion time not recorded", "status": "done", "assignee_id": "member-b"},
    ]
    for index, task in enumerate(tasks):
        task.update({"created_by": "member-a", "updated_at": stamp(index), "area": ["server/src"], "note": "Literal task " + MALICIOUS})
        task.setdefault("depends_on", [])
    state = {
        "team": {"id": "localteam", "name": "Local snapshot", "settings": {}},
        "me": {"id": "member-a", "name": "agent-a"},
        "members": [
            {"id": "member-a", "name": "agent-a", "last_seen": stamp(), "status": {"activity": "working"}},
            {"id": "member-b", "name": "agent-b", "last_seen": stamp(4)},
        ],
        "tasks": tasks, "claims": [], "asks_for_me": [], "huddles_for_me": [],
        "huddles": [{"id": "huddle-local", "kind": "brainstorm", "topic": "Nebula huddle marker", "context": "Local design discussion", "status": "closed", "started_by": "member-a", "created_at": stamp(10), "invited": [], "decision": "Snapshot only"}],
        "prompt": {"state": "draft", "version": 1, "sections": [], "votes": {}},
        "last_event_id": 2,
    }
    return {
        "state": state,
        "events": [
            {"id": 1, "kind": "update", "member_id": "member-a", "created_at": stamp(15), "data": {"text": "Local snapshot ready"}},
            {"id": 2, "kind": "task_done", "member_id": "member-a", "created_at": stamp(), "data": {"key": "T-103"}},
        ],
        "transcript": {"messages": [{"id": "message-local", "sender": "agent-b", "recipient": "agent-a", "text": "Cosmic transcript marker " + MALICIOUS, "task_id": "T-101", "created_at": stamp(12)}], "patches": [], "audit": [], "last_event_id": 1},
        "notes": [{"id": 21, "author_id": "member-a", "title": "Nebula note marker " + MALICIOUS, "body": "Snapshot knowledge", "tags": ["snapshot"], "created_at": stamp(8)}],
        "handoffs": [{"id": 11, "task_key": "T-101", "author_id": "member-a", "summary": "Orbit handoff marker " + MALICIOUS, "changed_files": ["server/example.py"], "open_questions": ["What remains?"], "next_step": "Verify snapshot", "created_at": stamp(7)}],
    }


def snapshot_handler(snapshot, requests):
    template = files("agentmesh_server").joinpath("dashboard.html").read_text(encoding="utf-8")
    page = compose_dashboard(template).replace("__AGENTMESH_NONCE__", NONCE).encode()
    assert CREDENTIAL.encode() not in page

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def session_key(self):
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie["agentmesh_session"].value if "agentmesh_session" in cookie else ""

        def do_POST(self):
            route = urlsplit(self.path).path
            requests.append(route)
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status, data, cookie = 200, {}, None
            if self.headers.get("Origin") != "http://" + self.headers.get("Host", ""):
                status, data = 403, {"error": "Same-origin request required"}
            elif route == "/auth/logout":
                cookie = "agentmesh_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"
                data = {"ok": True}
            elif route == "/auth/login":
                try:
                    key = json.loads(body).get("key")
                except (ValueError, AttributeError):
                    key = None
                if key == CREDENTIAL:
                    cookie = "agentmesh_session=" + CREDENTIAL + "; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000"
                    data = {"team_id": "localteam"}
                else:
                    status, data = 401, {"error": "The local snapshot access key was rejected"}
                    cookie = "agentmesh_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"
            else:
                status, data = 404, {"error": "No such local snapshot route"}
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = urlsplit(self.path).path
            requests.append(route)
            status, content_type = 200, "application/json; charset=utf-8"
            if route == "/":
                data, content_type = page, "text/html; charset=utf-8"
            elif route == "/v1/dashboard/state":
                # The member-only snapshot has no optional legacy dashboard gate.
                status, data = 401, {"error": "This snapshot uses member access"}
            elif route.startswith("/api/") and self.session_key() != CREDENTIAL:
                # The browser must use its HttpOnly cookie, never a Bearer key.
                status, data = 401, {"error": "The local snapshot access key was rejected"}
            elif route == "/api/state":
                data = snapshot["state"]
            elif route == "/api/events":
                data = {"events": snapshot["events"]}
            elif route == "/api/legacy/transcript":
                data = snapshot["transcript"]
            elif route == "/api/handoffs":
                data = {"handoffs": snapshot["handoffs"]}
            elif route == "/api/notes":
                data = {"notes": snapshot["notes"]}
            elif route == "/__fixture/advance":
                # Test control only: incoming local records are seen on the next poll.
                if snapshot["state"]["last_event_id"] == 2:
                    now = datetime.now(timezone.utc).isoformat()
                    snapshot["state"]["tasks"][0].update(status="assigned", assignee_id="member-a", updated_at=now)
                    ask = {"id": "ask-local", "from_id": "member-b", "to_id": "member-a", "question": "Photon question marker", "created_at": now}
                    snapshot["state"]["asks_for_me"] = [ask]
                    snapshot["events"].extend([
                        {"id": 3, "kind": "task_taken", "member_id": "member-a", "created_at": now, "data": {"key": "T-101", "assignee_id": "member-a"}},
                        {"id": 4, "kind": "ask", "member_id": "member-b", "created_at": now, "data": {"ask_id": "ask-local", "to_id": "member-a", "question": ask["question"]}},
                    ])
                    snapshot["transcript"]["messages"].append({"id": "message-incoming", "sender": "agent-b", "recipient": "agent-a", "text": "Incoming photon message", "task_id": "T-101", "created_at": now})
                    snapshot["transcript"]["last_event_id"] = 2
                    snapshot["state"]["last_event_id"] = 4
                elif snapshot["state"]["last_event_id"] == 4:
                    now = datetime.now(timezone.utc).isoformat()
                    snapshot["state"]["tasks"][0].update(status="in_progress", updated_at=now)
                    snapshot["events"].append({"id": 5, "kind": "task_started", "member_id": "member-a", "created_at": now, "data": {"key": "T-101"}})
                    snapshot["transcript"]["messages"].append({"id": "message-reduced", "sender": "agent-b", "recipient": "agent-a", "text": "Reduced motion message", "task_id": "T-101", "created_at": now})
                    snapshot["transcript"]["last_event_id"] = 3
                    snapshot["state"]["last_event_id"] = 5
                data = {"advanced": True}
            elif route == "/api/stream":
                status, data = 503, {"error": "Static fixture has no live stream"}
            elif route == "/favicon.ico":
                status, data = 204, b""
            else:
                status, data = 404, {"error": "No such local snapshot route"}
            body = data if isinstance(data, bytes) else json.dumps(copy.deepcopy(data)).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if route == "/":
                self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{NONCE}'; style-src 'nonce-{NONCE}' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def test_dashboard_polish_in_real_chrome():
    requests = []
    http = ThreadingHTTPServer(("127.0.0.1", 0), snapshot_handler(home_snapshot(), requests))
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        # Workspace-only short paths avoid Chrome's Unix socket path limit.
        with tempfile.TemporaryDirectory(prefix=".dashboard-polish-", dir=SERVER) as temporary, chrome_temporary_directory() as chrome_tmp:
            root = Path(temporary)
            profile = root / "profile"
            downloads = root / "downloads"
            downloads.mkdir()
            environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(root), "TMPDIR": str(chrome_tmp), "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            chrome_binary = shutil.which("google-chrome") or shutil.which("chromium")
            assert chrome_binary, "Headless Chrome is required for dashboard acceptance"
            with (root / "chrome.log").open("w+") as log:
                chrome = subprocess.Popen([
                    chrome_binary, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                    "--disable-background-networking", "--disable-component-update", "--disable-default-apps", "--disable-extensions", "--disable-sync", "--disable-breakpad", "--disable-crash-reporter", "--disable-domain-reliability", "--no-first-run", "--no-default-browser-check",
                    "--disable-features=MediaRouter,OptimizationHints,Translate,AutofillServerCommunication", "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
                    f"--user-data-dir={profile}", f"--disk-cache-dir={root / 'cache'}", f"--crash-dumps-dir={root / 'crashes'}", "about:blank",
                ], cwd=SERVER, env=environment, stdout=subprocess.DEVNULL, stderr=log)
                try:
                    deadline = time.monotonic() + 15
                    active_port = profile / "DevToolsActivePort"
                    while not active_port.exists():
                        assert chrome.poll() is None, "Headless Chrome exited before becoming ready"
                        assert time.monotonic() < deadline, "Headless Chrome did not become ready"
                        time.sleep(0.05)
                    settings = {"debugPort": int(active_port.read_text().splitlines()[0]), "baseUrl": f"http://localhost:{http.server_port}", "credential": CREDENTIAL, "downloads": str(downloads), "malicious": MALICIOUS}
                    result = subprocess.run([find_node(), str(HARNESS)], input=json.dumps(settings), text=True, capture_output=True, cwd=SERVER, env=environment, timeout=180, check=False)
                    output = (result.stdout + result.stderr).replace(CREDENTIAL, "[redacted]").strip()
                    assert result.returncode == 0, output or "Real Chrome dashboard acceptance failed"
                    assert "dashboard-polish-chrome-ok" in output
                finally:
                    if chrome.poll() is None:
                        chrome.terminate()
                        try:
                            chrome.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            chrome.kill()
                            chrome.wait(timeout=5)
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Local snapshot server did not stop"
    assert "/api/state" in requests and "/api/legacy/transcript" in requests
    assert "/auth/login" in requests and "/auth/logout" in requests
    assert not any(route.startswith("/v1/") and route != "/v1/dashboard/state" for route in requests), "Only the existing root login probe may use the legacy gate"
