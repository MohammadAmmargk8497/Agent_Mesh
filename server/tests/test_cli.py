import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import time


def cli(*args, env=None):
    return subprocess.run([sys.executable, "-m", "agentmesh_server", *args],
                          capture_output=True, text=True, env=env, timeout=10, check=False)


def test_init_config_permissions_refusal_force_and_env_override(tmp_path):
    config_dir = tmp_path / "new-config"
    env = {**os.environ, "AGENTMESH_SERVER_DIR": str(config_dir)}
    result = cli("init", env=env)
    assert result.returncode == 0, result.stderr
    config_path = config_dir / "config"
    original = config_path.read_bytes()
    config = json.loads(original)
    assert set(config) >= {"AGENT_A_TOKEN", "AGENT_B_TOKEN", "VIEWER_TOKEN"}
    tokens = [config[key] for key in ("AGENT_A_TOKEN", "AGENT_B_TOKEN", "VIEWER_TOKEN")]
    assert all(len(token) >= 32 for token in tokens)
    assert len(set(tokens)) == 3
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert "AGENTMESH_URL=" in result.stdout
    assert "AGENTMESH_TOKEN=" in result.stdout
    refused = cli("init", "--config-dir", str(config_dir))
    assert refused.returncode != 0
    assert config_path.read_bytes() == original
    forced = cli("init", "--config-dir", str(config_dir), "--force")
    assert forced.returncode == 0, forced.stderr
    assert config_path.read_bytes() != original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_clean_init_and_serve_health_and_database_permissions(tmp_path):
    config_dir = tmp_path / "clean"
    initialized = cli("init", "--config-dir", str(config_dir))
    assert initialized.returncode == 0, initialized.stderr
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen([sys.executable, "-m", "agentmesh_server", "serve", "--config-dir", str(config_dir),
                                "--host", "127.0.0.1", "--port", str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while True:
            assert process.poll() is None, process.communicate()[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read()) == {"ok": True, "service": "agentmesh"}
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.02)
            finally:
                connection.close()
        assert stat.S_IMODE((config_dir / "data.db").stat().st_mode) == 0o600
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_serve_non_loopback_without_flag_is_rejected_before_binding(tmp_path):
    config_dir = tmp_path / "remote"
    assert cli("init", "--config-dir", str(config_dir)).returncode == 0
    result = cli("serve", "--config-dir", str(config_dir), "--host", "0.0.0.0", "--port", "0")
    assert result.returncode != 0
    assert "--allow-remote-bind" in result.stderr
