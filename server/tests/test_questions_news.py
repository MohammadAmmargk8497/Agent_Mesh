import uuid

import pytest
from agentmesh_server.state import DAY, now_ms


def ask(api, text="Which interface should I use?", role="a", **extra):
    response = api.post("/v1/questions", {"to": "agent-b" if role == "a" else "agent-a", "text": text, **extra}, role)
    assert response.status == 201, response.body
    return response.json()["id"]


def commit(api, role="a", **extra):
    response = api.post("/v1/commits", {"sha": "abc1234", "subject": "Publish skeleton", **extra}, role)
    assert response.status == 201, response.body
    return response.json()


def test_questions_answer_later_only_once_and_only_by_recipient(api):
    task_id = api.task()
    question_id = ask(api, task_id=task_id)
    for role in ("a", "b"):
        rows = api.get("/v1/questions?open=true", role).json()["questions"]
        assert len(rows) == 1
        assert rows[0]["id"] == question_id
        assert rows[0]["sender"] == "agent-a"
        assert rows[0]["recipient"] == "agent-b"
        assert rows[0]["task_id"] == task_id
        assert rows[0]["answer"] is None
    root = f"/v1/questions/{question_id}/answer"
    assert api.post(root, {"text": "Cannot answer for them"}).status == 403
    assert api.post(root, {"text": "Use version one"}, "b").status == 200
    assert api.post(root, {"text": "Change answer"}, "b").status == 409
    for role in ("a", "b"):
        assert api.get("/v1/questions?open=true", role).json() == {"questions": []}
        answered = api.get("/v1/questions", role).json()["questions"][0]
        assert answered["answer"] == "Use version one"
        assert answered["answered_at"] >= answered["created_at"]
        news = api.get("/v1/news?peek=true", role).json()["news"]
        assert any(row["ref"] == question_id and row["actor"] == "agent-a" for row in news)
        assert any(row["ref"] == question_id and row["actor"] == "agent-b" for row in news)
    events = api.get("/v1/events?after=0").json()["events"]
    assert any(row["ref"] == question_id and row["actor"] == "agent-b" for row in events)


def test_question_and_answer_text_are_cleaned(api):
    question_id = ask(api, "\ufeff What\x00 now? \ufeff")
    assert api.post(f"/v1/questions/{question_id}/answer", {"text": "Use\x85 the\n\tcontract"}, "b").status == 200
    row = api.get("/v1/questions").json()["questions"][0]
    assert row["text"] == "What now?"
    assert row["answer"] == "Use the\n\tcontract"


@pytest.mark.parametrize("payload", [
    {"to": "agent-a", "text": "self"}, {"to": "viewer", "text": "viewer"},
    {"to": "agent-b", "text": "\x00"}, {"to": "agent-b", "text": "x" * 8001},
    {"to": "agent-b", "text": "😀" * 4001}, {"to": "agent-b", "text": "q", "task_id": "bad"},
])
def test_question_validation(api, payload):
    assert api.post("/v1/questions", payload).status == 400


def test_question_answer_validation_and_missing_question(api):
    question_id = ask(api)
    for text in ("", "\x00", "x" * 8001, False):
        assert api.post(f"/v1/questions/{question_id}/answer", {"text": text}, "b").status == 400
    assert api.post(f"/v1/questions/{uuid.uuid4()}/answer", {"text": "answer"}, "b").status == 404


def test_news_has_independent_server_cursors_and_peek_does_not_consume(api):
    assert api.get("/v1/news").json() == {"news": [], "unseen_count": 0}
    first = commit(api)
    for _ in range(2):
        peek = api.get("/v1/news?peek=true").json()
        assert peek["unseen_count"] == 1
        assert len(peek["news"]) == 1
    received = api.get("/v1/news").json()["news"]
    assert received[0]["id"] == first["id"]
    assert api.get("/v1/news").json() == {"news": [], "unseen_count": 0}
    assert api.get("/v1/news?peek=true", "b").json()["unseen_count"] == 1
    second = commit(api, "b", sha="def5678", subject="Implement skeleton")
    assert [row["id"] for row in api.get("/v1/news").json()["news"]] == [second["id"]]
    both = api.get("/v1/news", "b").json()["news"]
    assert [row["id"] for row in both] == [first["id"], second["id"]]
    assert all(row["kind"] == "commit" for row in both)


def test_news_pagination_is_chronological_and_does_not_skip_unread_rows(api):
    state = api.server.state
    with state.lock:
        for index in range(105):
            state.db.execute("INSERT INTO news (actor,kind,ref,summary,created_at) VALUES (?,?,?,?,?)",
                             ("agent-a", "commit", f"{index:07x}", "Commit announced", now_ms()))
    peek = api.get("/v1/news?peek=true", "b").json()
    assert peek["unseen_count"] == 105
    assert len(peek["news"]) == 100
    first = api.get("/v1/news", "b").json()["news"]
    assert len(first) == 100
    assert [row["id"] for row in first] == sorted(row["id"] for row in first)
    remaining = api.get("/v1/news?peek=true", "b").json()
    assert remaining["unseen_count"] == 5
    second = api.get("/v1/news", "b").json()["news"]
    assert len(second) == 5
    assert min(row["id"] for row in second) > max(row["id"] for row in first)


def test_commit_announcement_authorship_sanitization_and_event(api):
    result = commit(api, sha="a" * 40, subject="\ufeff Ship\x00 contract \ufeff", branch=" feature/\x85api ", files=["src/api.py"])
    row = api.get("/v1/news?peek=true", "b").json()["news"][0]
    assert row["id"] == result["id"]
    assert row["actor"] == "agent-a"
    assert row["kind"] == "commit"
    assert row["ref"] == "a" * 40
    assert "Ship contract" in row["summary"]
    assert "\x00" not in row["summary"] and "\x85" not in row["summary"]
    assert any(row["type"] == "commit" and row["ref"] == "a" * 40 for row in api.get("/v1/events?after=0", "b").json()["events"])


@pytest.mark.parametrize("extra", [
    {"sha": "abc123"}, {"sha": "g" * 40}, {"sha": "a" * 65},
    {"subject": "\x00"}, {"subject": "x" * 201},
    {"files": ["src/a.py"] * 51}, {"files": ["../secret"]}, {"files": ["src/\x00a.py"]},
])
def test_commit_validation(api, extra):
    assert api.post("/v1/commits", {"sha": "abc1234", "subject": "commit", **extra}).status == 400


def test_coordination_retention_preserves_open_and_boundary_records(api):
    old_huddle = api.post("/v1/huddles", {"kind": "review", "topic": "Old closed"}).json()["id"]
    open_huddle = api.post("/v1/huddles", {"kind": "review", "topic": "Old open"}).json()["id"]
    boundary_huddle = api.post("/v1/huddles", {"kind": "review", "topic": "Boundary"}).json()["id"]
    post_id = api.post(f"/v1/huddles/{old_huddle}/posts", {"kind": "idea", "text": "old post"}).json()["id"]
    for huddle_id in (old_huddle, boundary_huddle):
        api.post(f"/v1/huddles/{huddle_id}/close", {"decision": "closed"})
    old_question, open_question, boundary_question = ask(api), ask(api), ask(api)
    for question_id in (old_question, boundary_question):
        api.post(f"/v1/questions/{question_id}/answer", {"text": "answered"}, "b")
    api.post("/v1/master/sections", {"section": "Goal", "text": "Keep forever"})
    state = api.server.state
    now = now_ms()
    with state.lock:
        db = state.db
        db.execute("UPDATE huddles SET created_at=?", (now - 100 * DAY,))
        db.execute("UPDATE huddles SET closed_at=? WHERE id=?", (now - 90 * DAY - 1, old_huddle))
        db.execute("UPDATE huddles SET closed_at=? WHERE id=?", (now - 90 * DAY, boundary_huddle))
        db.execute("UPDATE questions SET created_at=?", (now - 100 * DAY,))
        db.execute("UPDATE questions SET answered_at=? WHERE id=?", (now - 30 * DAY - 1, old_question))
        db.execute("UPDATE questions SET answered_at=? WHERE id=?", (now - 30 * DAY, boundary_question))
        db.execute("UPDATE master_edits SET created_at=?", (now - 100 * DAY,))
        db.execute("DELETE FROM news")
        for ref, age in (("old", 30 * DAY + 1), ("boundary", 30 * DAY), ("fresh", 0)):
            db.execute("INSERT INTO news (actor,kind,ref,summary,created_at) VALUES (?,?,?,?,?)",
                       ("agent-a", "commit", ref, "Commit announced", now - age))
    state.sweep(now=now)
    with state.lock:
        assert {row["id"] for row in state.rows("SELECT id FROM huddles")} == {open_huddle, boundary_huddle}
        assert state.rows("SELECT id FROM huddle_posts WHERE id=?", post_id) == []
        assert {row["id"] for row in state.rows("SELECT id FROM questions")} == {open_question, boundary_question}
        assert {row["ref"] for row in state.rows("SELECT ref FROM news")} == {"boundary", "fresh"}
        assert len(state.rows("SELECT * FROM master_edits")) == 1
