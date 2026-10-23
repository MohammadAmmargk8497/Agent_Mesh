"""The hub migration must preserve unchanged legacy agent credentials."""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager

from agentmesh_server.config import init_config
from agentmesh_server.http import create_server
from conftest import LocalAPI


@contextmanager
def running_hub(config_dir, config, **options):
    server = create_server(config_dir, port=0, public_url="https://hub.example.invalid", **options)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield LocalAPI(server, config)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_seed_hashes_defaults_and_restart_idempotence(tmp_path):
    directory = tmp_path / "seed"
    config = init_config(directory)
    config["AGENT_A_TOKEN"] = "a" * 40 + "," + "o" * 40
    (directory / "config").write_text(json.dumps(config))
    team_id = None
    for _ in range(2):
        with running_hub(directory, config) as api:
            assert api.server.hub.process is None
            for role, name in (("a", "agent-a"), ("b", "agent-b")):
                result = api.get("/api/state", role)
                assert result.status == 200, result.body
                state = result.json()
                assert state["me"]["name"] == name
                assert state["team"]["name"] == "default"
                if team_id is None:
                    team_id = state["team"]["id"]
                assert state["team"]["id"] == team_id
                assert api.get("/v1/whoami", role).json() == {"agent": name}
            assert api.get("/api/state", "o" * 40).status == 401
            assert api.get("/v1/whoami", "o" * 40).status == 200
            settings = state["team"]["settings"]
            assert settings["assign_mode"] == "hybrid"
            assert settings["claim_mode"] == "ask"
            assert settings["claim_ttl_min"] == 30
            with sqlite3.connect(directory / "hub.db") as db:
                rows = db.execute("SELECT name,role,token_hash FROM members ORDER BY name").fetchall()
                assert rows == [("agent-a", "owner", hashlib.sha256(("a" * 40).encode()).hexdigest()),
                                ("agent-b", "member", hashlib.sha256(config["AGENT_B_TOKEN"].encode()).hexdigest())]
                assert db.execute("SELECT count(*) FROM teams").fetchone()[0] == 1
                created, expires = db.execute("SELECT created_at,invite_expires_at FROM teams").fetchone()
                assert expires - created == 7 * 24 * 3600 * 1000
            assert (directory / "hub.db").stat().st_mode & 0o777 == 0o600


def test_seed_reconciles_first_rotation_token_without_duplicates(tmp_path):
    directory = tmp_path / "rotate"
    config = init_config(directory)
    old_token = config["AGENT_A_TOKEN"]
    with running_hub(directory, config) as api:
        initial = api.get("/api/state").json()
    new_token = "n" * 40
    config["AGENT_A_TOKEN"] = new_token + "," + old_token
    (directory / "config").write_text(json.dumps(config))
    with running_hub(directory, config) as api:
        state = api.get("/api/state").json()
        assert state["me"]["name"] == "agent-a"
        assert state["team"]["id"] == initial["team"]["id"]
        assert len(state["members"]) == 2
        assert api.get("/api/state", old_token).status == 401
        assert api.get("/v1/whoami", old_token).status == 200
        assert api.get("/v1/whoami", new_token).status == 200
        with sqlite3.connect(directory / "hub.db") as db:
            assert db.execute("SELECT token_hash FROM members WHERE name='agent-a'").fetchone()[0] == hashlib.sha256(new_token.encode()).hexdigest()
