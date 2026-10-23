"""Exercise the vendored hub through AgentMesh's real loopback HTTP boundary."""

import http.client
import json
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "src"))

from agentmesh_server.config import init_config
from agentmesh_server.http import create_server


@contextmanager
def local_server(directory, config=None):
    config = config or init_config(directory)
    server = create_server(directory, port=0, public_url="http://127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    def request(method, path, data=None, token=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection(*server.server_address[:2], timeout=15)
        try:
            connection.request(method, path, None if data is None else json.dumps(data), headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            if response.status != expected:
                # Do not print returned bearer tokens or complete request bodies.
                raise AssertionError(f"{method} {path}: expected {expected}, got {response.status}")
            return result
        finally:
            connection.close()

    try:
        yield server, config, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def main():
    with tempfile.TemporaryDirectory(prefix=".agentmesh-hub-e2e-", dir=ROOT) as temporary:
        directory = Path(temporary) / "config"
        with local_server(directory) as (_, config, call):
            for key, name in (("AGENT_A_TOKEN", "agent-a"), ("AGENT_B_TOKEN", "agent-b")):
                assert call("GET", "/api/state", token=config[key])["me"]["name"] == name
                assert call("GET", "/v1/whoami", token=config[key]) == {"agent": name}
            team = call("POST", "/api/teams", {"team_name": "E2E", "member_name": "Owner"})
            owner = team["token"]
            joined = call("POST", "/api/join", {"code": team["invite_url"], "member_name": "Peer"})
            peer = joined["token"]
            call("POST", "/api/post", {"kind": "update", "text": "loopback integration"}, peer)
            tasks = call("POST", "/api/plan", {"tasks": [{"title": "Test the integration", "area": ["src/**"]}]}, owner)
            key = tasks["tasks"][0]["key"]
            assert call("POST", "/api/task", {"action": "start", "key": key}, peer)["task"]["status"] == "in_progress"
            claims = call("POST", "/api/claims", {"paths": ["src/example.py"], "task_key": key}, peer)
            assert claims["claimed"] == ["src/example.py"]
            conflict = call("POST", "/api/claims", {"paths": ["src/example.py"]}, owner)
            assert len(conflict["conflicts"]) == 1
            assert call("POST", "/api/task", {"action": "done", "key": key}, peer)["task"]["status"] == "done"
            call("POST", "/api/members/remove", {"member": joined["member_id"]}, owner)
            call("GET", "/api/state", token=peer, expected=401)
            call("POST", "/api/team/delete", {}, owner)
            call("GET", "/api/state", token=owner, expected=401)
    print("Hub loopback e2e PASS (seed, create, join, post, task, claim, remove, delete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
