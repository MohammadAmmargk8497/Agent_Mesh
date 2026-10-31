"""Standalone, loopback-only home-server and real Chrome team-feature check.

Run from server/: PYTHONPATH=src python3 tests/team_features_e2e.py
No production configuration, services, dependencies or remote APIs are used.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from agentmesh_server.config import init_config
from agentmesh_server.http import create_server
from agentmesh_server.hub import find_node
from conftest import LocalAPI


SERVER = Path(__file__).resolve().parents[1]


@contextmanager
def local_server(directory, config):
    server = create_server(directory, port=0, public_url="http://127.0.0.1")
    server.hub.explicit_public_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield LocalAPI(server, config)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Local server did not stop"


def response_json(response, label):
    assert response.status == 200, f"{label}: HTTP {response.status}"
    return response.json()


@contextmanager
def chrome_temporary_directory():
    # Chrome's SingletonSocket must fit Linux's 108-byte Unix-socket path limit.
    for index in range(256):
        directory = SERVER / f".c{index:02x}"
        try:
            directory.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("No short workspace directory available for Chrome")
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def browser_check(api, root, chrome_tmp, expected):
    profile = root / "chrome-profile"
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(root),
                   "TMPDIR": str(chrome_tmp), "XDG_CONFIG_HOME": str(root / "chrome-config"),
                   "XDG_CACHE_HOME": str(root / "chrome-cache")}
    chrome_log = (root / "chrome.log").open("w+")
    chrome = subprocess.Popen([
        "/usr/bin/google-chrome", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
        "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
        "--disable-extensions", "--disable-sync", "--disable-breakpad", "--disable-crash-reporter",
        "--disable-domain-reliability", "--no-first-run", "--no-default-browser-check",
        "--disable-features=MediaRouter,OptimizationHints,Translate,AutofillServerCommunication",
        "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
        f"--user-data-dir={profile}", f"--disk-cache-dir={root / 'chrome-cache'}",
        f"--crash-dumps-dir={root / 'chrome-crashes'}", "about:blank",
    ], cwd=SERVER, env=environment, stdout=subprocess.DEVNULL, stderr=chrome_log)
    try:
        deadline = time.monotonic() + 15
        active_port = profile / "DevToolsActivePort"
        while not active_port.exists():
            if chrome.poll() is not None:
                chrome_log.seek(0)
                raise AssertionError("Headless Chrome exited before becoming ready: " + chrome_log.read()[:4000])
            assert time.monotonic() < deadline, "Headless Chrome did not become ready"
            time.sleep(0.05)
        port = int(active_port.read_text().splitlines()[0])
        token = api.tokens["b"]
        settings = {"debugPort": port, "baseUrl": f"http://{api.host}:{api.port}",
                    "teamId": expected["teamId"], "token": token, **expected}
        result = subprocess.run([find_node(), str(Path(__file__).with_name("team_features_chrome.cjs"))],
                                input=json.dumps(settings), capture_output=True, text=True,
                                cwd=SERVER, env=environment, timeout=90, check=False)
        # Credentials only cross stdin/CDP in memory; never print them, including on failures.
        output = (result.stdout + result.stderr).replace(token, "[redacted]").strip()
        assert result.returncode == 0, output or "Real Chrome verification failed"
        print(output)
    finally:
        if chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=8)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=5)
        chrome_log.close()


def main():
    with tempfile.TemporaryDirectory(prefix=".agentmesh-e2e-", dir=SERVER) as temporary, chrome_temporary_directory() as chrome_tmp:
        root = Path(temporary)
        directory = root / "home"
        config = init_config(directory)
        title = 'Local note <img id="e2e-injected-note" src=x onerror="window.__teamNotesExecuted=true">'
        body = "Persistent shared note body"
        summary = '<script id="e2e-injected-handoff">window.__teamNotesExecuted=true</script>'
        with local_server(directory, config) as api:
            state = response_json(api.get("/api/state", "b"), "member state")
            team = state["team"]["id"]
            response_json(api.post("/api/plan", {"tasks": [{"key": "LOCAL", "title": "Local handoff task"}]}, "b"), "plan")
            note = response_json(api.post("/api/notes", {"title": title, "body": body, "tags": ["e2e", "home"]}, "b"), "create note")["note"]
            other = response_json(api.post("/api/notes", {"title": "Different note", "body": "Other body", "tags": ["other"]}, "b"), "create second note")["note"]
            handoff = response_json(api.post("/api/handoff", {"task_key": "LOCAL", "summary": summary,
                "changed_files": ["server/tests/team_features_e2e.py"], "open_questions": ["Review next release?"],
                "next_step": "Verify in the browser"}, "b"), "create handoff")["handoff"]
            assert response_json(api.get("/api/handoffs?task_key=LOCAL", "b"), "read handoff")["handoffs"] == [handoff]
            assert {row["id"] for row in response_json(api.get("/api/notes", "b"), "read notes")["notes"]} == {note["id"], other["id"]}
        # A new Python front door and new Node hub use only the same local SQLite files.
        with local_server(directory, config) as api:
            assert response_json(api.get("/api/state", "b"), "restarted state")["team"]["id"] == team
            assert response_json(api.get("/api/handoffs?task_key=LOCAL", "b"), "restarted handoff")["handoffs"] == [handoff]
            assert note in response_json(api.get("/api/notes", "b"), "restarted notes")["notes"]
            html = api.get("/t/" + team, None)
            assert html.status == 200
            assert all(value.encode() not in html.body for value in config.values()), "Credential embedded in HTML"
            browser_check(api, root, chrome_tmp, {"teamId": team, "taskKey": "LOCAL", "noteId": note["id"],
                "handoffId": handoff["id"], "title": title, "body": body, "summary": summary})
            notes = response_json(api.get("/api/notes", "b"), "notes after browser actions")["notes"]
            assert {row["id"] for row in notes} == {note["id"], other["id"]}
            handoffs = response_json(api.get("/api/handoffs?task_key=LOCAL", "b"), "handoffs after browser actions")["handoffs"]
            assert len(handoffs) == 2 and handoffs[0]["summary"] == "Browser-created handoff"
        print("Local home-server E2E passed: member APIs, full restart, real-page rendering, filters and explicit deletion")


if __name__ == "__main__":
    main()
