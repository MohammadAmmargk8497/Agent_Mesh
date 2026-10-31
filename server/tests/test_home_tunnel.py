"""Staging tunnel checks use a fake cloudflared, never the network or real keys."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "home_tunnel.sh"
TUNNEL_ID = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def tunnel(tmp_path):
    home = tmp_path / "home"
    directory = home / ".cloudflared"
    directory.mkdir(parents=True, mode=0o700)
    credentials = directory / f"{TUNNEL_ID}.json"
    credentials.write_text("{}")  # Deliberately not a real credential.
    credentials.chmod(0o600)
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "cloudflared"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['STUB_LOG']).write_text(json.dumps({\n"
        "    'args': sys.argv[1:],\n"
        "    'tunnel_settings': [key for key in os.environ if key.startswith('TUNNEL_')]\n"
        "}))\n"
    )
    stub.chmod(0o700)
    env = dict(os.environ, HOME=str(home), PATH=f"{binary}:{os.environ['PATH']}",
               STUB_LOG=str(tmp_path / "calls.json"))
    return directory, credentials, env


def run_tunnel(env, *arguments):
    return subprocess.run(["bash", str(SCRIPT), *arguments], env=env, text=True,
                          capture_output=True, timeout=10, check=False)


def test_tunnel_only_runs_fixed_staging_config(tunnel):
    directory, credentials, env = tunnel
    production_config = directory / "config.yml"
    production_config.write_text("existing unrelated tunnel config\n")
    # A remote token or ambient origin must not override the local config.
    env.update(TUNNEL_TOKEN="fixture-not-a-token", TUNNEL_URL="https://unrelated.invalid")
    result = run_tunnel(env, "--tunnel-id", TUNNEL_ID.upper())
    assert result.returncode == 0, result.stderr
    config = directory / "agentmesh-home.yml"
    assert config.read_text() == (
        f"tunnel: {TUNNEL_ID}\n"
        f"credentials-file: {json.dumps(str(credentials))}\n"
        "ingress:\n"
        "  - hostname: agentmesh-home.zahid.win\n"
        "    service: http://127.0.0.1:8787\n"
        "  - service: http_status:404\n"
    )
    assert config.stat().st_mode & 0o777 == 0o600
    assert production_config.read_text() == "existing unrelated tunnel config\n"
    assert credentials.read_text() == "{}"
    assert json.loads(Path(env["STUB_LOG"]).read_text()) == {
        "args": ["tunnel", "--config", str(config), "run", TUNNEL_ID],
        "tunnel_settings": [],
    }
    assert "agents.zahid.win" not in config.read_text()


def test_tunnel_custom_credentials_inside_private_directory(tunnel):
    directory, credentials, env = tunnel
    renamed = directory / 'staging "quoted" key.json'
    credentials.rename(renamed)
    result = run_tunnel(env, "--tunnel-id", TUNNEL_ID, "--credentials-file", str(renamed))
    assert result.returncode == 0, result.stderr
    assert f"credentials-file: {json.dumps(str(renamed))}\n" in (directory / "agentmesh-home.yml").read_text()


@pytest.mark.parametrize("arguments", [
    [], ["--tunnel-id"], ["--tunnel-id", "not-a-uuid"],
    ["--tunnel-id", TUNNEL_ID, "--hostname", "agents.zahid.win"],
    ["--tunnel-id", TUNNEL_ID, "--url", "http://other.invalid"],
    ["--tunnel-id", TUNNEL_ID, "--config", "/unrelated/config.yml"],
])
def test_tunnel_rejects_invalid_arguments_and_overrides(tunnel, arguments):
    directory, _, env = tunnel
    result = run_tunnel(env, *arguments)
    assert result.returncode == 2
    assert not (directory / "agentmesh-home.yml").exists()
    assert not Path(env["STUB_LOG"]).exists()


@pytest.mark.parametrize("unsafe", ["outside", "symlink", "public-file", "public-directory", "config-symlink", "missing"])
def test_tunnel_rejects_unsafe_credentials_or_config(tunnel, unsafe):
    directory, credentials, env = tunnel
    selected = credentials
    outside = directory.parent / "other.json"
    outside.write_text("{}")
    outside.chmod(0o600)
    if unsafe == "outside":
        selected = outside
    elif unsafe == "symlink":
        credentials.unlink()
        credentials.symlink_to(outside)
    elif unsafe == "public-file":
        credentials.chmod(0o644)
    elif unsafe == "public-directory":
        directory.chmod(0o755)
    elif unsafe == "config-symlink":
        (directory / "agentmesh-home.yml").symlink_to(outside)
    else:
        credentials.unlink()
    result = run_tunnel(env, "--tunnel-id", TUNNEL_ID, "--credentials-file", str(selected))
    assert result.returncode == 2
    assert outside.read_text() == "{}"
    assert not Path(env["STUB_LOG"]).exists()


def test_tunnel_help_does_not_prepare_or_connect(tunnel):
    directory, _, env = tunnel
    result = run_tunnel(env, "--help")
    assert result.returncode == 0
    assert "agentmesh-home.zahid.win" in result.stdout
    assert not (directory / "agentmesh-home.yml").exists()
    assert not Path(env["STUB_LOG"]).exists()
