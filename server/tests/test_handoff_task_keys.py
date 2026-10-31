"""Permanent handoff history must not attach to a reused task key."""

import sqlite3

import pytest

from agentmesh_server.config import init_config
from test_hub_seed import running_hub


@pytest.mark.parametrize("lagging_counter", [False, True])
def test_replacement_reserves_archived_handoff_task_keys(tmp_path, lagging_counter):
    directory = tmp_path / "handoff-task-keys"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        created = api.post("/api/plan", {"tasks": [{"key": "T1", "title": "Original task"}]})
        assert created.status == 200, created.body
        original_key = created.json()["tasks"][0]["key"]
        assert original_key == "T1"
        posted = api.post("/api/handoff", {
            "task_key": original_key, "summary": "Original task history",
            "changed_files": [], "open_questions": [], "next_step": "Review original work",
        })
        assert posted.status == 200, posted.body
        archived_handoff = posted.json()["handoff"]

        if lagging_counter:
            team_id = api.get("/api/state").json()["team"]["id"]
            with sqlite3.connect(directory / "hub.db") as db:
                db.execute("UPDATE teams SET next_task=1 WHERE id=?", (team_id,))

        replaced = api.post("/api/plan", {
            "replace": True, "tasks": [{"key": original_key, "title": "Unrelated replacement"}],
        })
        assert replaced.status == 200, replaced.body
        replacement_key = replaced.json()["tasks"][0]["key"]
        assert replacement_key != original_key
        assert api.get("/api/handoffs?task_key=" + original_key).json() == {
            "handoffs": [archived_handoff]}
        assert api.get("/api/handoffs?task_key=" + replacement_key).json() == {"handoffs": []}
        tasks = api.get("/api/state").json()["tasks"]
        assert [task["key"] for task in tasks] == [replacement_key]
