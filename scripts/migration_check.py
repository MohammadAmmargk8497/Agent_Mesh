"""Upgrade a copied, populated legacy-shaped fixture; never open a live database."""

import ast
import hashlib
import json
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "src"))

from agentmesh_server import coordination
from agentmesh_server.config import init_config
from agentmesh_server.state import now_ms
from e2e_hub import local_server


def legacy_schema():
    # Reuse the must-pass test's frozen pre-coordination schema, not today's
    # production SCHEMA: otherwise this check could silently stop upgrading.
    tree = ast.parse((ROOT / "server" / "tests" / "test_migration.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "OLD_SCHEMA" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("frozen legacy schema not found")


def main():
    with tempfile.TemporaryDirectory(prefix=".agentmesh-migration-", dir=ROOT) as temporary:
        directory = Path(temporary) / "config"
        config = init_config(directory)
        source = Path(temporary) / "legacy-shaped.db"
        task_id = str(uuid.uuid4())
        at = now_ms()
        with sqlite3.connect(source) as legacy:
            legacy.executescript(legacy_schema())
            legacy.executescript(coordination.SCHEMA)
            legacy.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", (
                task_id, "Preserved task", "Original description", "agent-b", "agent-a", "open",
                json.dumps(["preserved criterion"]), None, at, at,
            ))
            legacy.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (
                "legacy-message", "agent-a", "agent-b", "Keep this message", task_id, at, None,
            ))
            legacy.execute("INSERT INTO master_sections VALUES (?,?,?,?,?,?)", (
                "Goal", "Keep coordination data", "agent-a", 1,
                hashlib.sha256(b"Keep coordination data").hexdigest(), at,
            ))
            legacy.execute("INSERT INTO file_claims VALUES (?,?,?,?,?)", (
                "src/legacy.py", "agent-a", task_id, at + 30 * 60_000, at,
            ))
            legacy.execute("INSERT INTO news (actor,kind,ref,summary,created_at) VALUES (?,?,?,?,?)", (
                "agent-a", "legacy", task_id, "Preserved news", at,
            ))
            legacy.commit()
            snapshots = {table: legacy.execute(f"SELECT * FROM {table}").fetchall()
                         for table in ("messages", "master_sections", "file_claims", "news")}
            # A real SQLite backup copy tests the normal on-disk upgrade path,
            # without reading any user's config or production database.
            with sqlite3.connect(directory / "data.db") as copy:
                legacy.backup(copy)
        source.chmod(0o600)
        for _ in range(2):
            with local_server(directory, config) as (_, _, call):
                task = call("GET", "/v1/tasks/" + task_id, token=config["AGENT_B_TOKEN"])["task"]
                assert task["title"] == "Preserved task"
                assert task["acceptance_criteria"] == ["preserved criterion"]
                assert task["created_at"] == task["updated_at"] == at
                assert task["owner"] is None and task["file_areas"] == []
                state = call("GET", "/api/state", token=config["AGENT_A_TOKEN"])
                assert state["team"]["name"] == "default" and state["me"]["name"] == "agent-a"
                assert len(state["members"]) == 2
            with sqlite3.connect(directory / "data.db") as upgraded:
                for table, rows in snapshots.items():
                    assert upgraded.execute(f"SELECT * FROM {table}").fetchall() == rows, table
    print("Migration check PASS (copied legacy task/message/coordination rows preserved; hub seed idempotent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
