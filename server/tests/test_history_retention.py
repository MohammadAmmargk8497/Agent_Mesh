import sqlite3

import pytest

from agentmesh_server import coordination
from agentmesh_server.state import DAY, SCHEMA, State


NOW = 500 * DAY
OLD = NOW - 400 * DAY
HISTORY_TABLES = (
    "events", "audit", "messages", "patches", "tasks", "master_sections",
    "master_edits", "master_state", "master_votes", "huddles", "huddle_posts",
    "huddle_votes", "dashboard_tasks", "questions", "news", "news_details",
    "news_seen", "member_settings",
)


def seed_history(db):
    def insert(table, **values):
        db.execute(f"INSERT INTO {table} ({','.join(values)}) "
                   f"VALUES ({','.join('?' for _ in values)})", tuple(values.values()))

    for status in ("open", "in_progress", "done", "blocked", "accepted", "cancelled"):
        insert("tasks", id=status, title="Historical task", description="Keep description",
               assignee="agent-b", created_by="agent-a", status=status,
               acceptance_criteria='["Keep criteria"]', result="Keep result",
               created_at=OLD, updated_at=OLD)
    insert("events", recipient="agent-b", type="message", ref="message",
           actor="agent-a", created_at=OLD)
    insert("audit", actor="agent-a", action="message.send", ref="message", created_at=OLD)
    for name, acked_at in (("acked", OLD), ("unacked", None)):
        insert("messages", id=name, sender="agent-a", recipient="agent-b",
               text="Keep message", task_id="accepted", created_at=OLD, acked_at=acked_at)
    insert("patches", id="patch", sender="agent-a", title="Keep patch",
           text="Keep diff", task_id="accepted", created_at=OLD)
    insert("master_sections", section="Goal", text="Keep current prompt", author="agent-a",
           revision=2, sha256="current-hash", created_at=OLD)
    for revision in (1, 2):
        insert("master_edits", id=f"edit-{revision}", section="Goal",
               text=f"Keep prompt version {revision}", author="agent-a",
               revision=revision, sha256=f"hash-{revision}", created_at=OLD)
    insert("master_votes", round=1, agent="agent-b", decision="approve", note="Keep vote")
    for status in ("open", "closed"):
        insert("huddles", id=status, owner="agent-a", kind="review", topic="Keep huddle",
               status=status, decision="Keep decision" if status == "closed" else None,
               created_at=OLD, closed_at=OLD if status == "closed" else None)
    for huddle_id in ("closed", "missing-huddle"):
        insert("huddle_posts", id=f"post-{huddle_id}", huddle_id=huddle_id,
               author="agent-a", kind="idea", text="Keep post", created_at=OLD)
        insert("huddle_votes", huddle_id=huddle_id, ref_post_id=f"post-{huddle_id}",
               agent="agent-b", vote="agree", note="Keep huddle vote",
               created_at=OLD, updated_at=OLD)
    for task_id in ("open", "missing-task"):
        insert("dashboard_tasks", task_id=task_id, state="open")
    for name, answered_at in (("answered", OLD), ("unanswered", None)):
        insert("questions", id=name, sender="agent-a", recipient="agent-b",
               text="Keep question", task_id="accepted", created_at=OLD,
               answer="Keep answer" if answered_at is not None else None,
               answered_at=answered_at)
    insert("news", id=1, actor="agent-a", kind="commit", ref="commit",
           summary="Keep news", created_at=OLD)
    for news_id in (1, 999):
        insert("news_details", news_id=news_id, data='{"text":"Keep details"}')
    insert("news_seen", agent="agent-a", last_id=1)
    insert("member_settings", agent="agent-b", auto_respond=0)
    for path, expires_at in (("expired.py", NOW - 1), ("boundary.py", NOW),
                             ("active.py", NOW + DAY)):
        insert("file_claims", path=path, agent="agent-a", task_id="open",
               expires_at=expires_at, created_at=OLD)


def history(db):
    return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in HISTORY_TABLES}


@pytest.mark.parametrize("entrypoint", ["coordination", "state", "startup"])
def test_every_sweep_retains_backdated_history_and_only_expires_claims(
        tmp_path, monkeypatch, entrypoint):
    config_dir = tmp_path / "state"
    config_dir.mkdir()
    with sqlite3.connect(config_dir / "data.db") as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        coordination.migrate(db)
        seed_history(db)
        before = history(db)
        active_claim = dict(db.execute(
            "SELECT * FROM file_claims WHERE path='active.py'").fetchone())
    db.close()

    # Leave claims untouched at startup when testing the other sweep entrypoints.
    monkeypatch.setattr("agentmesh_server.state.now_ms",
                        lambda: NOW if entrypoint == "startup" else OLD - DAY)
    config = {"AGENT_A_TOKEN": "a" * 32, "AGENT_B_TOKEN": "b" * 32,
              "VIEWER_TOKEN": "v" * 32}
    state = State(config_dir, config)
    try:
        with state.lock:
            if entrypoint == "coordination":
                assert coordination.sweep(state.db, NOW) == {"file_claims": 2}
            elif entrypoint == "state":
                assert state.sweep(now=NOW) == {"file_claims": 2}
            assert history(state.db) == before
            assert state.rows("SELECT * FROM file_claims") == [active_claim]
            # Repeated maintenance must not change either history or active claims.
            assert state.sweep(now=NOW) == {"file_claims": 0}
            assert history(state.db) == before
            assert state.rows("SELECT * FROM file_claims") == [active_claim]
    finally:
        state.close()
