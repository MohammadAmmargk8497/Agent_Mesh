"""Unattended startup preserves agent credentials without writing raw secrets."""

import hashlib
import json
import sqlite3

import pytest
from agentmesh_server.config import init_config, load_config
from agentmesh_server.http import create_server
from test_hub_seed import running_hub


@pytest.fixture
def env_config(monkeypatch):
    config = {"AGENT_A_TOKEN": "environment-agent-a-" + "a" * 32,
              "AGENT_B_TOKEN": "environment-agent-b-" + "b" * 32}
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("VIEWER_TOKEN", raising=False)
    # LocalAPI retains a viewer test credential, but the server must not need it.
    return {**config, "VIEWER_TOKEN": "unused-viewer-" + "v" * 32}


def stored_hashes(directory):
    with sqlite3.connect(directory / "data.db") as db:
        return db.execute("SELECT agent, token_hash FROM agent_tokens ORDER BY agent, token_hash").fetchall()


def expected_hashes(config):
    return sorted((name, hashlib.sha256(token.strip().encode()).digest())
                  for name, key in (("agent-a", "AGENT_A_TOKEN"), ("agent-b", "AGENT_B_TOKEN"))
                  for token in config[key].split(","))


def test_env_first_start_hashes_hub_auth_restart_and_no_plaintext(tmp_path, env_config, caplog, capsys):
    directory = tmp_path / "fresh"
    team = None
    for _ in range(2):
        with running_hub(directory, env_config) as api:
            assert api.server.server_address[0] == "127.0.0.1"
            assert api.get("/health", None).status == 200
            for role, name in (("a", "agent-a"), ("b", "agent-b")):
                assert api.get("/v1/whoami", role).json() == {"agent": name}
                response = api.get("/api/state", role)
                assert response.status == 200, response.body
                state = response.json()
                assert state["me"]["name"] == name
                team = team or state["team"]["id"]
                assert state["team"]["id"] == team
                assert len(state["members"]) == 2
            assert api.get("/v1/whoami", "viewer").status == 401
            assert stored_hashes(directory) == expected_hashes(env_config)
            with sqlite3.connect(directory / "hub.db") as db:
                assert db.execute("SELECT name, token_hash FROM members ORDER BY name").fetchall() == [
                    (name, digest.hex()) for name, digest in expected_hashes(env_config)]
                assert db.execute("SELECT count(*) FROM teams").fetchone()[0] == 1
        assert not (directory / "config").exists()
        output = capsys.readouterr()
        for key in ("AGENT_A_TOKEN", "AGENT_B_TOKEN"):
            token = env_config[key]
            assert token not in output.out + output.err + caplog.text
            for path in directory.iterdir():
                if path.is_file():
                    assert token.encode() not in path.read_bytes()


def test_env_rotation_reconciles_hashes_and_revokes_removed_token(tmp_path, env_config, monkeypatch):
    directory = tmp_path / "rotated"
    old_token = env_config["AGENT_A_TOKEN"]
    with running_hub(directory, env_config) as api:
        original = api.get("/api/state").json()
    new_token = "rotated-environment-agent-a-" + "n" * 32
    for value in (new_token + "," + old_token, new_token):
        monkeypatch.setenv("AGENT_A_TOKEN", value)
        config = {**env_config, "AGENT_A_TOKEN": value}
        with running_hub(directory, config) as api:
            state = api.get("/api/state").json()
            assert state["team"]["id"] == original["team"]["id"]
            assert state["me"]["id"] == original["me"]["id"]
            assert len(state["members"]) == 2
            assert api.get("/v1/whoami", new_token).status == 200
            assert api.get("/v1/whoami", old_token).status == (200 if "," in value else 401)
            assert api.get("/api/state", old_token).status == 401
            assert api.get("/v1/whoami", "b").status == 200
            assert stored_hashes(directory) == expected_hashes(config)


def test_env_overrides_disk_without_rewriting_private_config(tmp_path, env_config):
    directory = tmp_path / "existing"
    local = init_config(directory)
    contents = (directory / "config").read_bytes()
    with running_hub(directory, env_config, hub=False) as api:
        assert api.get("/v1/whoami").status == 200
        assert api.get("/v1/whoami", local["AGENT_A_TOKEN"]).status == 401
    assert (directory / "config").read_bytes() == contents
    assert json.loads(contents) == local


@pytest.mark.parametrize("values", [
    {"AGENT_A_TOKEN": "a" * 40},
    {"AGENT_B_TOKEN": "b" * 40},
    {"AGENT_A_TOKEN": "", "AGENT_B_TOKEN": "b" * 40},
    {"AGENT_A_TOKEN": "too-short", "AGENT_B_TOKEN": "b" * 40},
    {"AGENT_A_TOKEN": "a" * 40 + ",short", "AGENT_B_TOKEN": "b" * 40},
    {"AGENT_A_TOKEN": "a" * 40, "AGENT_B_TOKEN": "a" * 40},
    {"AGENT_A_TOKEN": "a" * 40 + "," + "b" * 40, "AGENT_B_TOKEN": "b" * 40},
])
def test_invalid_env_fails_safely_without_disk_fallback(tmp_path, monkeypatch, values):
    directory = tmp_path / "invalid"
    init_config(directory)
    for key in ("AGENT_A_TOKEN", "AGENT_B_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError) as failure:
        create_server(directory, port=0, hub=False)
    assert not (directory / "data.db").exists()
    for value in values.values():
        if value:
            assert value not in str(failure.value)


def test_missing_env_and_config_still_requires_local_init(tmp_path, monkeypatch):
    for key in ("AGENT_A_TOKEN", "AGENT_B_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="config not found"):
        load_config(tmp_path / "missing")


def test_env_does_not_relax_explicit_remote_bind(tmp_path, env_config):
    with pytest.raises(ValueError, match="--allow-remote-bind"):
        create_server(tmp_path / "bind", host="0.0.0.0", port=0)
