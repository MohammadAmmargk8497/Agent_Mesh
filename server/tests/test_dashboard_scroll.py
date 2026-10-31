"""Real Chrome scroll regression using only a generated, loopback home snapshot."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from agentmesh_server.hub import find_node
from team_features_e2e import chrome_temporary_directory
from test_dashboard_polish import CREDENTIAL, SERVER, home_snapshot, snapshot_handler

EVIDENCE = Path("/tmp/scrollfix-verify")


def long_home_snapshot():
    snapshot = home_snapshot()
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    tasks = snapshot["state"]["tasks"]
    for column, status in enumerate(["open", "assigned", "in_progress", "done"]):
        for index in range(22):
            tasks.append({
                "key": f"SCROLL-{column}-{index}", "title": f"Scroll fixture {status} task {index}",
                "status": status, "assignee_id": "member-a" if column else None,
                "created_by": "member-a", "created_at": stamp, "updated_at": stamp,
                "note": "Representative local task with enough content to overflow its column.",
                "depends_on": [], "area": ["server/tests"],
            })
    for index in range(65):
        snapshot["transcript"]["messages"].append({
            "id": f"scroll-message-{index}", "sender": "agent-a", "recipient": "agent-b",
            "text": f"Scroll fixture transcript message {index}\nLocal snapshot content only.",
            "task_id": "T-101", "created_at": stamp,
        })
    snapshot["transcript"]["audit"] = [
        {"id": index, "actor": "agent-a", "action": "message_sent", "ref": str(index), "created_at": stamp}
        for index in range(40)
    ]
    snapshot["transcript"]["patches"] = [
        {"id": str(index), "sender": "agent-b", "title": f"Local patch {index}", "task_id": "T-101", "created_at": stamp}
        for index in range(40)
    ]
    snapshot["events"].extend(
        {"id": index + 10, "kind": "update", "member_id": "member-a", "created_at": stamp,
         "data": {"text": f"Local scroll event {index}"}}
        for index in range(50)
    )
    snapshot["state"]["claims"] = [
        {"path": f"server/tests/local_{index}.py", "member_id": "member-a", "task_key": "T-101",
         "expires_at": (now + timedelta(hours=1)).isoformat()}
        for index in range(30)
    ]
    snapshot["state"]["asks_for_me"] = [
        {"id": f"scroll-question-{index}", "from_id": "member-b", "to_id": "member-a",
         "question": f"Local scroll question {index}", "created_at": stamp}
        for index in range(12)
    ]
    return snapshot


@pytest.mark.parametrize("mode", ["member", "legacy"])
def test_dashboard_scroll_survives_real_refreshes(mode):
    snapshot, requests = long_home_snapshot(), []
    snapshot["state"]["huddles"][0]["post_count"] = 1
    base = snapshot_handler(snapshot, requests)
    stopping = threading.Event()
    evidence = EVIDENCE / mode
    legacy = {
        "me": "agent-a", "last_event_id": 1, "events": [],
        "messages": snapshot["transcript"]["messages"], "patches": snapshot["transcript"]["patches"],
        "audit": snapshot["transcript"]["audit"], "master_prompt": {}, "huddles": [],
        "tasks": [{**task, "id": task["key"], "description": task.get("note", ""),
                   "assignee": "agent-a", "created_by": "agent-a"} for task in snapshot["state"]["tasks"]],
        "claims": [{**claim, "agent": "agent-a", "task_id": "T-101"} for claim in snapshot["state"]["claims"]],
        "questions": [{"id": question["id"], "sender": "agent-b", "recipient": "agent-a",
                       "text": question["question"], "created_at": question["created_at"]}
                      for question in snapshot["state"]["asks_for_me"]],
        "news": [{**event, "actor": "agent-a", "summary": event["data"].get("text", "Local event"), "ref": "T-101"}
                 for event in snapshot["events"]],
    }

    class Handler(base):
        def json_response(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = urlsplit(self.path).path
            if mode == "legacy" and route == "/v1/dashboard/state" and self.session_key() == CREDENTIAL:
                requests.append(route)
                legacy["last_event_id"] += 1
                version = legacy["last_event_id"]
                legacy["messages"][-1]["text"] = f"Refreshed local transcript version {version}"
                legacy["tasks"][0]["updated_at"] = datetime.now(timezone.utc).isoformat()
                legacy["questions"][0]["text"] = f"Local question refreshed {version}"
                legacy["news"][-1]["summary"] = f"Local news refreshed {version}"
                legacy["audit"][-1]["action"] = f"local_refresh_{version}"
                legacy["patches"][-1]["title"] = f"Local patch refresh {version}"
                self.json_response(legacy)
                return
            if route == "/api/stream" and self.session_key() == CREDENTIAL:
                requests.append(route)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while not stopping.is_set():
                        self.wfile.write(b": local fixture heartbeat\n\n")
                        self.wfile.flush()
                        stopping.wait(1)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if route == "/api/huddle" and self.session_key() == CREDENTIAL:
                requests.append(route)
                self.json_response({"posts": [{"seq": 1, "kind": "comment", "member_id": "member-a",
                                               "text": "Local discussion stays disclosed", "created_at": datetime.now(timezone.utc).isoformat()}]})
                return
            # Every local transcript poll changes the content, forcing a real redraw.
            if route == "/api/legacy/transcript" and self.session_key() == CREDENTIAL:
                version = snapshot["transcript"]["last_event_id"] + 1
                snapshot["transcript"]["last_event_id"] = version
                snapshot["transcript"]["messages"][-1]["text"] = f"Refreshed local transcript version {version}"
                snapshot["transcript"]["audit"][-1]["action"] = f"local_refresh_{version}"
                snapshot["transcript"]["patches"][-1]["title"] = f"Local patch refresh {version}"
            super().do_GET()

    evidence.mkdir(parents=True, exist_ok=True)
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix=".dashboard-scroll-", dir=SERVER) as temporary, chrome_temporary_directory() as chrome_tmp:
            root = Path(temporary)
            profile, downloads = root / "profile", root / "downloads"
            downloads.mkdir()
            environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(root), "TMPDIR": str(chrome_tmp),
                           "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            chrome_binary = shutil.which("google-chrome") or shutil.which("chromium")
            assert chrome_binary, "Headless Chrome is required for scroll acceptance"
            with (evidence / "scroll-chrome.log").open("w+") as log:
                chrome = subprocess.Popen([
                    chrome_binary, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                    "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
                    "--disable-extensions", "--disable-sync", "--disable-breakpad", "--disable-crash-reporter",
                    "--disable-domain-reliability", "--no-first-run", "--no-default-browser-check",
                    "--disable-features=MediaRouter,OptimizationHints,Translate,AutofillServerCommunication",
                    "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
                    f"--user-data-dir={profile}", f"--disk-cache-dir={root / 'cache'}",
                    f"--crash-dumps-dir={root / 'crashes'}", "about:blank",
                ], cwd=SERVER, env=environment, stdout=subprocess.DEVNULL, stderr=log)
                try:
                    deadline = time.monotonic() + 15
                    active_port = profile / "DevToolsActivePort"
                    while not active_port.exists():
                        assert chrome.poll() is None, "Headless Chrome exited before becoming ready"
                        assert time.monotonic() < deadline, "Headless Chrome did not become ready"
                        time.sleep(0.05)
                    settings = {"debugPort": int(active_port.read_text().splitlines()[0]),
                                "baseUrl": f"http://localhost:{http.server_port}", "credential": CREDENTIAL,
                                "downloads": str(downloads), "evidence": str(evidence), "mode": mode,
                                "tasks": len(snapshot["state"]["tasks"])}
                    result = subprocess.run([find_node(), str(Path(__file__).with_name("dashboard_scroll_chrome.cjs"))],
                                            input=json.dumps(settings), text=True, capture_output=True,
                                            cwd=SERVER, env=environment, timeout=120, check=False)
                    output = (result.stdout + result.stderr).replace(CREDENTIAL, "[redacted]").strip()
                    (evidence / "scroll-result.txt").write_text(output + "\n")
                    assert result.returncode == 0, output or "Real Chrome scroll acceptance failed"
                    assert "dashboard-scroll-chrome-ok" in output
                finally:
                    if chrome.poll() is None:
                        chrome.terminate()
                        try:
                            chrome.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            chrome.kill()
                            chrome.wait(timeout=5)
    finally:
        stopping.set()
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)
        (evidence / "scroll-http-requests.json").write_text(json.dumps(requests, indent=2) + "\n")
        assert not thread.is_alive(), "Local snapshot server did not stop"
    if mode == "legacy":
        assert requests.count("/v1/dashboard/state") >= 4
    else:
        assert requests.count("/api/state") >= 7
        assert requests.count("/api/legacy/transcript") >= 7
