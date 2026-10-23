"""The config boundary exercises copied code only, against disposable loopback hubs."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentmesh_mcp import launcher

TOKEN = "synthetic-legacy-token-" + "x" * 40
ACCESS_ID = "synthetic-access-id-" + "i" * 40
ACCESS_SECRET = "synthetic-access-secret-" + "s" * 40
VENDOR = Path(launcher.VENDOR)


@pytest.fixture
def fake_hub():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, dict(self.headers), None))
            if self.path.startswith("/api/stream"):
                data = b'data: {"id":1,"kind":"post"}\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                data = json.dumps({"team": {"id": "tabcdef", "name": "default", "settings": {}},
                                   "me": {"id": "mabcdef", "name": "agent-a"},
                                   "members": [], "tasks": [], "events": [], "last_event_id": 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            calls.append((self.path, dict(self.headers), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def synthetic_env(tmp_path, hub, access):
    """Never inherit real AgentMesh credentials, home files, or git command behavior."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AGENTMESH_", "OPENHUDDLE_"))}
    config = tmp_path / "synthetic-settings"
    config.write_text(f"AGENTMESH_URL={hub}\nAGENTMESH_TOKEN={TOKEN}\nAGENTMESH_AGENT_NAME=agent-a\n" +
                      (f"AGENTMESH_CF_ACCESS_CLIENT_ID={ACCESS_ID}\nAGENTMESH_CF_ACCESS_CLIENT_SECRET={ACCESS_SECRET}\n" if access else ""))
    config.chmod(0o600)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    git = binaries / "git"
    git.write_text("#!/bin/sh\nexit 1\n")
    git.chmod(0o700)
    node = shutil.which("node")
    assert node, "Node >=20 is a required client prerequisite"
    env.update({"HOME": str(tmp_path), "OPENHUDDLE_HOME": str(tmp_path / "openhuddle"),
                "AGENTMESH_CONFIG": str(config), "AGENTMESH_NODE": node,
                "PATH": str(binaries) + os.pathsep + env.get("PATH", "")})
    return env, config


@pytest.mark.parametrize("access", [False, True])
def test_unchanged_config_starts_vendored_mcp_with_access_headers(tmp_path, fake_hub, access):
    hub, calls = fake_hub
    env, config = synthetic_env(tmp_path, hub, access)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "team_post", "arguments": {"kind": "update", "text": "Synthetic test update"}}},
    ]
    original = config.read_bytes()
    result = subprocess.run([sys.executable, "-m", "agentmesh_mcp.launcher"], cwd=tmp_path, env=env,
                            input="".join(json.dumps(msg) + "\n" for msg in messages),
                            capture_output=True, text=True, check=False, timeout=20)
    assert result.returncode == 0, result.stderr
    replies = {item["id"]: item for item in map(json.loads, result.stdout.splitlines())}
    assert replies[1]["result"]["serverInfo"]["name"] == "openhuddle"
    assert {tool["name"] for tool in replies[2]["result"]["tools"]} == {
        "team_sync", "team_post", "team_ask", "team_reply", "huddle", "master_prompt", "plan", "task", "claim", "team_settings"}
    assert replies[3]["result"]["isError"] is False
    assert config.read_bytes() == original  # Existing config.env never needs rewriting.
    saved = tmp_path / "openhuddle/.openhuddle/config.json"
    saved_data = json.loads(saved.read_text())
    assert saved_data["team_id"] == "tabcdef" and saved_data["member_id"] == "mabcdef"
    assert saved_data["token"] == TOKEN and len(saved_data["machine"]) == 32
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert {path.split("?")[0] for path, _, _ in calls} >= {"/api/state", "/api/heartbeat", "/api/post"}
    for _, headers, _ in calls:
        lower = {key.lower(): value for key, value in headers.items()}
        assert lower.get("authorization") == "Bearer " + TOKEN
        assert lower.get("cf-access-client-id") == (ACCESS_ID if access else None)
        assert lower.get("cf-access-client-secret") == (ACCESS_SECRET if access else None)
    assert TOKEN not in result.stdout + result.stderr
    assert ACCESS_SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("access", [False, True])
def test_same_fetch_boundary_covers_sse_and_does_not_leak_to_other_origin(tmp_path, fake_hub, access):
    hub, calls = fake_hub
    env, _ = synthetic_env(tmp_path, hub, access)
    if access:
        env.update(AGENTMESH_CF_ACCESS_CLIENT_ID=ACCESS_ID, AGENTMESH_CF_ACCESS_CLIENT_SECRET=ACCESS_SECRET)
    script = f'''
      import {{ installAccessFetch }} from {json.dumps((VENDOR / 'src/client/agentmesh-boundary.js').as_uri())};
      import {{ Api }} from {json.dumps((VENDOR / 'src/client/api.js').as_uri())};
      const hub = {json.dumps(hub)};
      installAccessFetch(hub);
      const seen = [];
      await new Api(hub, {json.dumps(TOKEN)}).get('/api/state');
      await new Api(hub, {json.dumps(TOKEN)}).stream(0, event => seen.push(event));
      // localhost is a different origin from 127.0.0.1, but reaches the same fake hub.
      await fetch(hub.replace('127.0.0.1', 'localhost') + '/other-origin');
      if (seen.length !== 1 || seen[0].kind !== 'post') process.exit(2);
    '''
    result = subprocess.run([env["AGENTMESH_NODE"], "--input-type=module", "-e", script], cwd=tmp_path,
                            env=env, capture_output=True, text=True, check=False, timeout=20)
    assert result.returncode == 0, result.stderr
    assert [path for path, _, _ in calls] == ["/api/state", "/api/stream?since=0", "/other-origin"]
    for path, headers, _ in calls:
        lower = {key.lower(): value for key, value in headers.items()}
        expected = access and path != "/other-origin"
        assert lower.get("cf-access-client-id") == (ACCESS_ID if expected else None)
        assert lower.get("cf-access-client-secret") == (ACCESS_SECRET if expected else None)


def test_missing_or_old_node_is_a_single_clear_error_without_fallback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTMESH_CONFIG", str(tmp_path / "absent-settings"))
    monkeypatch.setattr(launcher.shutil, "which", lambda _: None)
    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="v18.20.0\n"))
    monkeypatch.setenv("AGENTMESH_NODE", "synthetic-old-node")
    monkeypatch.setattr(launcher.os, "execve", lambda *a: pytest.fail("must not fall back or execute an old node"))
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err.count("requires Node.js >= 20") == 1
    assert "No legacy fallback" in output.err


def test_launcher_refuses_symlink_settings(monkeypatch, tmp_path):
    target = tmp_path / "synthetic-settings"
    target.write_text("AGENTMESH_TOKEN=" + TOKEN)
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    monkeypatch.setenv("AGENTMESH_CONFIG", str(alias))
    with pytest.raises(ValueError, match="symbolic links"):
        launcher.load_settings()


@pytest.mark.parametrize("access", [False, True])
def test_wire_installed_bin_keeps_config_and_access_bootstrap(tmp_path, fake_hub, access):
    hub, calls = fake_hub
    env, _ = synthetic_env(tmp_path, hub, access)
    install_script = f'''import {{ installApp }} from {json.dumps((VENDOR / 'src/client/wire.js').as_uri())};
                        console.log(installApp());'''
    installed = subprocess.run([env["AGENTMESH_NODE"], "--input-type=module", "-e", install_script],
                               cwd=tmp_path, env=env, capture_output=True, text=True, check=False, timeout=10)
    assert installed.returncode == 0, installed.stderr
    executable = Path(installed.stdout.strip())
    assert executable.is_file() and executable.is_relative_to(tmp_path)
    message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    result = subprocess.run([env["AGENTMESH_NODE"], str(executable), "mcp"], cwd=tmp_path, env=env,
                            input=message, capture_output=True, text=True, check=False, timeout=20)
    assert result.returncode == 0, result.stderr
    tools = json.loads(result.stdout)["result"]["tools"]
    assert "team_sync" in {tool["name"] for tool in tools}
    assert {path for path, _, _ in calls} >= {"/api/state", "/api/heartbeat"}
    for _, headers, _ in calls:
        lower = {key.lower(): value for key, value in headers.items()}
        assert lower["authorization"] == "Bearer " + TOKEN
        assert lower.get("cf-access-client-id") == (ACCESS_ID if access else None)
        assert lower.get("cf-access-client-secret") == (ACCESS_SECRET if access else None)


@pytest.mark.parametrize("entrypoint", ["launcher", "vendored-bin"])
@pytest.mark.parametrize("project_source", ["cwd", "CLAUDE_PROJECT_DIR", "--dir", "--dir="])
def test_project_membership_wins_over_legacy_env_fallback(tmp_path, fake_hub, project_source, entrypoint):
    hub, calls = fake_hub
    env, _ = synthetic_env(tmp_path, hub.replace("127.0.0.1", "localhost"), True)
    project = tmp_path / "project"
    project.mkdir()
    project_token = "vc_" + "project-membership-" * 3
    script = f'''import {{ saveConfig }} from {json.dumps((VENDOR / 'src/client/config.js').as_uri())};
      saveConfig({json.dumps(str(project))}, {{hub: {json.dumps(hub)}, token: {json.dumps(project_token)},
        team_id: 'tother', member_id: 'mother', team_name: 'Other team', member_name: 'other'}});'''
    saved = subprocess.run([env["AGENTMESH_NODE"], "--input-type=module", "-e", script], cwd=project,
                           env=env, capture_output=True, text=True, check=False, timeout=10)
    assert saved.returncode == 0, saved.stderr
    if project_source == "CLAUDE_PROJECT_DIR":
        env["CLAUDE_PROJECT_DIR"] = str(project)
    directory_args = (["--dir", str(project)] if project_source == "--dir"
                      else ["--dir=" + str(project)] if project_source == "--dir=" else [])
    command = ([sys.executable, "-m", "agentmesh_mcp.launcher"] if entrypoint == "launcher"
               else [env["AGENTMESH_NODE"], str(VENDOR / "bin/openhuddle.js"), "mcp"])
    result = subprocess.run([*command, *directory_args],
                            cwd=project if project_source == "cwd" else tmp_path, env=env,
                            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
                            capture_output=True, text=True, check=False, timeout=20)
    assert result.returncode == 0, result.stderr
    assert calls and all(path != "/api/state" for path, _, _ in calls)
    assert all(headers.get("authorization") == "Bearer " + project_token for _, headers, _ in calls)
    assert all({key.lower(): value for key, value in headers.items()}.get("cf-access-client-id") == ACCESS_ID
               for _, headers, _ in calls)
    assert all({key.lower(): value for key, value in headers.items()}.get("cf-access-client-secret") == ACCESS_SECRET
               for _, headers, _ in calls)
    assert not (tmp_path / "openhuddle/.openhuddle/config.json").exists()
