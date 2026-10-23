from __future__ import annotations

import http.client
import json
import threading
import time
from dataclasses import dataclass

import pytest


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self):
        return json.loads(self.body)


class LocalAPI:
    def __init__(self, server, config):
        self.server = server
        self.config = config
        self.host, self.port = server.server_address[:2]
        self.tokens = {
            "a": config["AGENT_A_TOKEN"].split(",")[0],
            "b": config["AGENT_B_TOKEN"].split(",")[0],
            "viewer": config["VIEWER_TOKEN"].split(",")[0],
        }

    def request(self, method, path, role="a", data=None, *, body=None, headers=None, timeout=5):
        outgoing = dict(headers or {})
        if role is not None:
            outgoing["Authorization"] = f"Bearer {self.tokens.get(role, role)}"
        if data is not None:
            body = json.dumps(data).encode()
            outgoing.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=outgoing)
            result = connection.getresponse()
            return Response(result.status, result.read(), {k.lower(): v for k, v in result.getheaders()})
        finally:
            connection.close()

    def get(self, path, role="a", **kwargs):
        return self.request("GET", path, role, **kwargs)

    def post(self, path, data=None, role="a", **kwargs):
        return self.request("POST", path, role, data, **kwargs)

    def task(self, assignee="agent-b", role="a"):
        result = self.post("/v1/tasks", {
            "title": "  test task  ", "description": "  test description  ",
            "assignee": assignee, "acceptance_criteria": ["covered by tests"],
        }, role)
        assert result.status == 201, result.body
        return result.json()["id"]


@pytest.fixture
def api(tmp_path):
    from agentmesh_server.config import init_config
    from agentmesh_server.http import create_server

    config_dir = tmp_path / "config"
    config = init_config(config_dir)
    server = create_server(config_dir, port=0, hub=False)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield LocalAPI(server, config)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true before timeout")
