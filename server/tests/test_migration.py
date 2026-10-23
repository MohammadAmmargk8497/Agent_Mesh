import json
import sqlite3
import threading
import uuid

import pytest
from agentmesh_server.config import init_config
from agentmesh_server.http import create_server
from agentmesh_server.state import now_ms
from conftest import LocalAPI

# Keep the pre-coordination schema literal: importing the current SCHEMA would
# silently stop exercising a real upgrade when that schema gains new columns.
OLD_SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
  assignee TEXT NOT NULL, created_by TEXT NOT NULL, status TEXT NOT NULL,
  acceptance_criteria TEXT NOT NULL, result TEXT, created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE messages (
  id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
  text TEXT NOT NULL, task_id TEXT, created_at INTEGER NOT NULL, acked_at INTEGER
);
CREATE TABLE patches (
  id TEXT PRIMARY KEY, sender TEXT NOT NULL, title TEXT NOT NULL,
  text TEXT NOT NULL, task_id TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL, type TEXT NOT NULL,
  ref TEXT NOT NULL, actor TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
  ref TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX events_recipient ON events(recipient, id);
"""


@pytest.mark.parametrize("partial", [False, True])
def test_old_schema_upgrades_in_place_and_restart_is_idempotent(tmp_path, partial):
    config_dir = tmp_path / "legacy"
    config = init_config(config_dir)
    task_id = str(uuid.uuid4())
    timestamp = now_ms()
    with sqlite3.connect(config_dir / "data.db") as db:
        db.executescript(OLD_SCHEMA)
        if partial:
            db.execute("ALTER TABLE tasks ADD COLUMN owner TEXT")
        db.execute("INSERT INTO tasks (id,title,description,assignee,created_by,status,acceptance_criteria,result,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (task_id, "Legacy task", "Keep this row", "agent-b", "agent-a", "open", json.dumps(["original criterion"]), None, timestamp, timestamp))
    for _ in range(2):
        server = create_server(config_dir, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            api = LocalAPI(server, config)
            result = api.get(f"/v1/tasks/{task_id}", "b")
            assert result.status == 200
            task = result.json()["task"]
            assert task["title"] == "Legacy task"
            assert task["acceptance_criteria"] == ["original criterion"]
            assert task["owner"] is None
            assert task["file_areas"] == task["depends_on"] == task["waiting_on"] == []
            assert task["contract"] is False
            assert task["created_at"] == task["updated_at"] == timestamp
            with server.state.lock:
                columns = {row["name"] for row in server.state.rows("PRAGMA table_info(tasks)")}
                assert {"owner", "file_areas", "depends_on", "contract"} <= columns
                assert len(server.state.rows("SELECT id FROM tasks")) == 1
            assert api.post("/v1/claims", {"path": "src/legacy.py"}).status == 201
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            assert not thread.is_alive()
