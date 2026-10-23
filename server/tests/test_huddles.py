import pytest


def start(api, kind="design", topic="Define the API", role="a"):
    result = api.post("/v1/huddles", {"kind": kind, "topic": topic}, role)
    assert result.status == 201, result.body
    return result.json()["id"]


def huddle(api, huddle_id):
    return api.get(f"/v1/huddles/{huddle_id}").json()["huddle"]


@pytest.mark.parametrize("kind", ["brainstorm", "design", "review", "standup"])
def test_huddle_kinds_posts_and_waiting_on(api, kind):
    huddle_id = start(api, kind)
    root = f"/v1/huddles/{huddle_id}"
    initial = huddle(api, huddle_id)
    assert initial["kind"] == kind
    assert initial["owner"] == "agent-a"
    assert initial["status"] == "open"
    assert initial["waiting_on"] == ["agent-a", "agent-b"]
    assert initial["posts"] == []
    idea = api.post(root + "/posts", {"kind": "idea", "text": "Use a typed contract"})
    assert idea.status == 201
    assert huddle(api, huddle_id)["waiting_on"] == ["agent-b"]
    back = api.post(root + "/posts", {"kind": "back", "text": "Agree", "ref_post_id": idea.json()["id"]}, "b")
    assert back.status == 201
    assert api.post(root + "/posts", {"kind": "push_back", "text": "Include errors", "ref_post_id": idea.json()["id"]}, "b").status == 201
    current = huddle(api, huddle_id)
    assert current["waiting_on"] == []
    assert [row["kind"] for row in current["posts"]] == ["idea", "back", "push_back"]
    assert current["posts"][1]["ref_post_id"] == idea.json()["id"]
    assert api.get("/v1/huddles?status=open", "b").json()["huddles"][0]["id"] == huddle_id


def test_owner_closes_huddle_announces_to_both_and_can_create_tasks(api):
    huddle_id = start(api)
    root = f"/v1/huddles/{huddle_id}"
    payload = {"decision": "Publish skeleton first", "tasks": [
        {"title": "Skeleton", "description": "Create API", "assignee": "agent-b", "file_areas": ["src/api.py"]},
    ]}
    assert api.post(root + "/close", payload, "b").status == 403
    result = api.post(root + "/close", payload)
    assert result.status == 200, result.body
    closed = huddle(api, huddle_id)
    assert closed["status"] == "closed"
    assert closed["decision"] == "Publish skeleton first"
    assert closed["closed_at"] > 0
    assert api.get("/v1/huddles?status=open").json()["huddles"] == []
    assert api.get("/v1/huddles?status=closed").json()["huddles"][0]["id"] == huddle_id
    assert api.post(root + "/posts", {"kind": "idea", "text": "too late"}, "b").status == 409
    assert api.post(root + "/close", {"decision": "again"}).status == 409
    tasks = api.get("/v1/tasks", "b").json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["owner"] == "agent-a"
    assert tasks[0]["file_areas"] == ["src/api.py"]
    for role in ("a", "b"):
        news = api.get("/v1/news?peek=true", role).json()["news"]
        assert any(row["kind"] == "huddle_closed" and row["ref"] == huddle_id for row in news)
        events = api.get("/v1/events?after=0", role).json()["events"]
        assert any(row["type"] == "huddle_closed" and row["ref"] == huddle_id for row in events)


def test_huddle_new_text_strips_controls(api):
    huddle_id = start(api, topic="\ufeff API\x00 design \ufeff")
    root = f"/v1/huddles/{huddle_id}"
    assert api.post(root + "/posts", {"kind": "idea", "text": "Use\x85 types\nnow"}, "b").status == 201
    assert api.post(root + "/close", {"decision": "Ship\x7f it"}).status == 200
    current = huddle(api, huddle_id)
    assert current["topic"] == "API design"
    assert current["posts"][0]["text"] == "Use types\nnow"
    assert current["decision"] == "Ship it"


@pytest.mark.parametrize("payload", [
    {"kind": "meeting", "topic": "t"}, {"kind": "design", "topic": "x" * 201},
    {"kind": "design", "topic": "😀" * 101}, {"kind": "design", "topic": "\x00"},
])
def test_huddle_creation_validation(api, payload):
    assert api.post("/v1/huddles", payload).status == 400


@pytest.mark.parametrize("payload", [
    {"kind": "vote", "text": "x"}, {"kind": "idea", "text": "x" * 8001},
    {"kind": "back", "text": "x", "ref_post_id": "not-a-uuid"},
])
def test_huddle_post_validation(api, payload):
    huddle_id = start(api)
    assert api.post(f"/v1/huddles/{huddle_id}/posts", payload, "b").status == 400


def test_huddle_reference_must_belong_to_same_huddle(api):
    one, two = start(api), start(api)
    post = api.post(f"/v1/huddles/{one}/posts", {"kind": "idea", "text": "one"}).json()["id"]
    assert api.post(f"/v1/huddles/{two}/posts", {"kind": "back", "text": "agree", "ref_post_id": post}, "b").status == 400


def test_huddle_close_validates_tasks_before_writing_anything(api):
    huddle_id = start(api)
    root = f"/v1/huddles/{huddle_id}"
    valid = {"title": "t", "description": "d", "assignee": "agent-b"}
    for tasks in ([valid] * 11, [valid, {**valid, "assignee": "viewer"}]):
        assert api.post(root + "/close", {"decision": "done", "tasks": tasks}).status == 400
        assert huddle(api, huddle_id)["status"] == "open"
        assert api.get("/v1/tasks").json()["tasks"] == []
