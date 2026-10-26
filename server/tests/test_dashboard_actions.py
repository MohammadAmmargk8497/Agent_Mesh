"""Authenticated dashboard-only projections and additive write actions."""

import uuid

import pytest

TASK_ACTIONS = ("take", "accept", "decline", "start", "release", "finish")
MISSING_ID = "00000000-0000-0000-0000-000000000000"
WRITE_PATHS = [f"/v1/dashboard/tasks/{MISSING_ID}/{action}" for action in TASK_ACTIONS] + [
    f"/v1/huddles/{MISSING_ID}/comments", f"/v1/huddles/{MISSING_ID}/votes",
]


def dashboard(api, role="a"):
    response = api.get("/v1/dashboard/state", role)
    assert response.status == 200, response.body
    return response.json()


def task(api, task_id, role="a"):
    return next(item for item in dashboard(api, role)["tasks"] if item["id"] == task_id)


def action(api, task_id, name, role="b", **body):
    return api.post(f"/v1/dashboard/tasks/{task_id}/{name}", body, role)


def start_huddle(api):
    response = api.post("/v1/huddles", {"kind": "brainstorm", "topic": "Choose a contract"})
    assert response.status == 201
    return response.json()["id"]


def post_idea(api, huddle_id):
    response = api.post(f"/v1/huddles/{huddle_id}/posts", {"kind": "idea", "text": "Use typed messages"})
    assert response.status == 201
    return response.json()["id"]


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_every_new_write_route_requires_agent_bearer(api, path):
    assert api.post(path, {}, None).status == 401
    assert api.post(path, {}, "invalid-token").status == 401
    assert api.post(path, {}, "viewer").status == 401


def test_dashboard_state_auth_shape_and_settings(api):
    assert api.get("/v1/dashboard/state", None).status == 401
    assert api.get("/v1/dashboard/state", "viewer").status == 401
    assert api.get("/v1/transcript").status == 403
    task_id = api.task()
    assert api.post("/v1/settings", {"auto_respond": False}, "b").status == 200
    state = dashboard(api, "b")
    assert api.get("/v1/transcript", "viewer").status == 401
    assert state["me"] == "agent-b"
    assert set(state) == {
        "me", "messages", "tasks", "patches", "audit", "events", "last_event_id",
        "master_prompt", "huddles", "claims", "questions", "news", "feed_details", "member_settings",
    }
    assert state["member_settings"] == [
        {"agent": "agent-a", "auto_respond": True},
        {"agent": "agent-b", "auto_respond": False},
    ]
    projected = task(api, task_id)
    assert projected["status"] == "open"
    assert projected["dashboard_status"] == "proposed"
    assert "dashboard_status" not in api.get(f"/v1/tasks/{task_id}").json()["task"]
    assert api.get("/v1/dashboard/state?after=no").status == 400
    assert api.get("/v1/dashboard/state?after=9007199254740992").status == 400
    assert api.get("/v1/dashboard/state?after=0").status == 200


def test_dashboard_agent_scope_cannot_read_or_take_private_records(api):
    private_task = api.task(assignee="agent-b", role="b")
    private_message, private_question = str(uuid.uuid4()), str(uuid.uuid4())
    state = api.server.state
    with state.condition:
        state.db.execute("INSERT INTO messages (id,sender,recipient,text,created_at) VALUES (?,'agent-b','agent-b','Private note',1)", (private_message,))
        state.db.execute("INSERT INTO questions (id,sender,recipient,text,created_at) VALUES (?,'agent-b','agent-b','Private question',1)", (private_question,))
        state.db.execute("INSERT INTO audit (actor,action,ref,created_at) VALUES ('agent-b','private',?,1)", (private_message,))
        state.db.execute("INSERT INTO events (recipient,type,ref,actor,created_at) VALUES ('agent-b','private',?,'agent-b',1)", (private_task,))
    own_state, other_state = dashboard(api, "b"), dashboard(api, "a")
    assert private_task in {item["id"] for item in own_state["tasks"]}
    assert private_message in {item["id"] for item in own_state["messages"]}
    assert private_question in {item["id"] for item in own_state["questions"]}
    for key in ("tasks", "messages", "questions", "audit", "events"):
        assert other_state[key] == []
    assert not any(item["ref"] == private_task for item in other_state["news"])
    assert any(item["ref"] == private_task for item in own_state["news"])
    assert any(item["ref"] == private_task for item in api.get("/v1/news?peek=true").json()["news"])
    for name in TASK_ACTIONS:
        assert action(api, private_task, name, "a").status == 404
    assert api.get("/v1/transcript", "viewer").status == 401
    assert private_question in {item["id"] for item in own_state["questions"]}


@pytest.mark.parametrize("name", TASK_ACTIONS)
@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"null"])
def test_dashboard_task_actions_always_require_json_object(api, name, body):
    task_id = api.task()
    assert api.post(f"/v1/dashboard/tasks/{task_id}/{name}", body=body, role="b").status == 400
    assert task(api, task_id)["dashboard_status"] == "proposed"


@pytest.mark.parametrize("name", TASK_ACTIONS)
def test_dashboard_task_actions_missing_task_and_wrong_method(api, name):
    path = f"/v1/dashboard/tasks/{MISSING_ID}/{name}"
    assert api.post(path, {}).status == 404
    assert api.get(path).status == 405


def test_accept_start_finish_preserve_legacy_states_and_release_claims(api):
    task_id = api.task()
    assert action(api, task_id, "accept").json() == {
        "id": task_id, "status": "open", "dashboard_status": "assigned",
    }
    assert api.get(f"/v1/tasks/{task_id}", "b").json()["task"]["status"] == "open"
    assert api.post("/v1/claims", {"path": "src/own.py", "task_id": task_id}, "b").status == 201
    assert api.post("/v1/claims", {"path": "src/other.py", "task_id": task_id}).status == 201
    assert action(api, task_id, "start").json()["dashboard_status"] == "in_progress"
    result = action(api, task_id, "finish", summary="  Complete\x00 with tests  ")
    assert result.json() == {"id": task_id, "status": "done", "dashboard_status": "done"}
    finished = task(api, task_id)
    assert finished["result"] == "Complete with tests"
    assert [claim["path"] for claim in dashboard(api)["claims"]] == ["src/other.py"]
    assert api.post(f"/v1/tasks/{task_id}/review", {"decision": "accept"}).status == 200
    assert task(api, task_id)["status"] == "accepted"
    news = dashboard(api)["news"]
    assert {item["kind"] for item in news} >= {"task_accepted", "task_claimed", "task_done"}
    assert any(item["type"] == "task_done" for item in dashboard(api)["events"])


def test_decline_take_release_can_reassign_and_open_the_task(api):
    task_id = api.task()
    assert action(api, task_id, "decline").json()["dashboard_status"] == "open"
    assert action(api, task_id, "take", "a").json()["dashboard_status"] == "assigned"
    assert task(api, task_id)["assignee"] == "agent-a"
    assert api.post("/v1/claims", {"path": "src/task.py", "task_id": task_id}).status == 201
    assert action(api, task_id, "release", "a").json()["dashboard_status"] == "open"
    assert dashboard(api)["claims"] == []
    assert task(api, task_id, "b")["dashboard_status"] == "open"
    assert action(api, task_id, "accept", "b").status == 404
    assert action(api, task_id, "take").status == 200
    assert action(api, task_id, "start").status == 200
    assert action(api, task_id, "release").json() == {
        "id": task_id, "status": "open", "dashboard_status": "open",
    }
    assert task(api, task_id)["assignee"] == "agent-b"
    assert {item["kind"] for item in dashboard(api)["news"]} >= {
        "task_declined", "task_taken", "task_released",
    }


def test_take_proposed_finish_default_summary_and_unblocks_dependencies(api):
    root = api.task()
    response = api.post("/v1/tasks", {
        "title": "Dependent", "description": "Use the result", "assignee": "agent-b", "depends_on": [root],
    })
    dependent = response.json()["id"]
    assert action(api, dependent, "accept").status == 200
    blocked = action(api, dependent, "start")
    assert blocked.status == 409
    assert blocked.json()["waiting_on"] == [root]
    assert action(api, root, "take").status == 200
    assert action(api, root, "start").status == 200
    assert action(api, root, "finish").status == 200
    assert task(api, root)["result"] == "Completed from dashboard"
    assert action(api, dependent, "start").status == 200
    assert any(event["type"] == "task_unblocked" and event["ref"] == dependent for event in dashboard(api, "b")["events"])


@pytest.mark.parametrize("start_route", ["dashboard", "legacy"])
def test_mixed_task_apis_do_not_keep_stale_assignment_on_reopen(api, start_route):
    task_id = api.task()
    assert action(api, task_id, "accept").status == 200
    if start_route == "dashboard":
        assert action(api, task_id, "start").status == 200
        assert action(api, task_id, "finish").status == 200
    else:
        assert api.post(f"/v1/tasks/{task_id}/claim", {}, "b").status == 200
        assert api.post(f"/v1/tasks/{task_id}/complete", {"summary": "Done"}, "b").status == 200
    assert api.post(f"/v1/tasks/{task_id}/review", {"decision": "reopen"}).status == 200
    assert task(api, task_id)["dashboard_status"] == "proposed"
    assert action(api, task_id, "accept").status == 200


@pytest.mark.parametrize("name", ["accept", "decline", "start", "release", "finish"])
def test_task_response_or_work_requires_assignee(api, name):
    task_id = api.task()
    assert action(api, task_id, name, "a").status == 403
    assert task(api, task_id)["dashboard_status"] == "proposed"


@pytest.mark.parametrize("name", TASK_ACTIONS)
def test_each_dashboard_task_action_rejects_bad_state(api, name):
    task_id = api.task()
    if name in ("take", "accept", "decline", "finish"):
        assert action(api, task_id, "accept").status == 200
    expected = "assigned" if name in ("take", "accept", "decline", "finish") else "proposed"
    assert action(api, task_id, name).status == 409
    assert task(api, task_id)["dashboard_status"] == expected


def test_take_cannot_steal_assigned_or_in_progress_tasks(api):
    task_id = api.task()
    assert action(api, task_id, "accept").status == 200
    assert action(api, task_id, "take", "a").status == 409
    assert action(api, task_id, "start").status == 200
    assert action(api, task_id, "take", "a").status == 409
    assert task(api, task_id)["assignee"] == "agent-b"


def test_dashboard_task_action_normalizes_case_insensitive_route_match(api):
    task_id = api.task()
    assert action(api, task_id, "ACCEPT").json()["dashboard_status"] == "assigned"
    assert action(api, task_id, "START").json()["status"] == "in_progress"
    assert action(api, task_id, "FINISH").json()["status"] == "done"


def test_vote_feed_details_keep_history_without_changing_news_response(api):
    for text, decision, note in [("First version", "approve", "Looks\x00 good"), ("Second version", "reject", "Needs more detail")]:
        assert api.post("/v1/master/sections", {"section": "Goal", "text": text}).status == 201
        assert api.post("/v1/master/submit", {}).status == 200
        assert api.post("/v1/master/vote", {"decision": decision, "note": note}, "b").status == 200
    state = dashboard(api)
    votes = [item for item in state["news"] if item["kind"] == "master_voted"]
    assert [state["feed_details"][str(item["id"])] for item in votes] == [
        {"decision": "approve", "note": "Looks good", "revision": 1},
        {"decision": "reject", "note": "Needs more detail", "revision": 2},
    ]
    assert dashboard(api, "b")["feed_details"] == state["feed_details"]
    for item in api.get("/v1/news?peek=true").json()["news"]:
        assert set(item) == {"id", "actor", "kind", "ref", "summary", "created_at"}


@pytest.mark.parametrize("summary", [None, False, "", "\x00", "x" * 8001, "😀" * 4001, "\ud800"])
def test_finish_summary_validation_keeps_state_and_claims(api, summary):
    task_id = api.task()
    assert action(api, task_id, "accept").status == 200
    assert action(api, task_id, "start").status == 200
    assert api.post("/v1/claims", {"path": "src/task.py", "task_id": task_id}, "b").status == 201
    assert action(api, task_id, "finish", summary=summary).status == 400
    assert task(api, task_id)["dashboard_status"] == "in_progress"
    assert len(dashboard(api)["claims"]) == 1


@pytest.mark.parametrize("endpoint", ["comments", "votes"])
def test_new_huddle_actions_missing_closed_and_wrong_method(api, endpoint):
    assert api.post(f"/v1/huddles/{MISSING_ID}/{endpoint}", {}).status == 404
    huddle_id = start_huddle(api)
    path = f"/v1/huddles/{huddle_id}/{endpoint}"
    assert api.get(path).status == 405
    assert api.post(f"/v1/huddles/{huddle_id}/close", {"decision": "Ship it"}).status == 200
    assert api.post(path, {"text": "late", "ref_post_id": str(uuid.uuid4()), "vote": "up"}, "b").status == 409


@pytest.mark.parametrize("kind,waiting", [("brainstorm", ["agent-b"]), ("design", ["agent-b"]), ("review", ["agent-b"]), ("standup", ["agent-a", "agent-b"])])
def test_dashboard_huddle_starter_counts_except_standup_without_changing_legacy(api, kind, waiting):
    response = api.post("/v1/huddles", {"kind": kind, "topic": "Plan"})
    huddle_id = response.json()["id"]
    assert dashboard(api)["huddles"][0]["waiting_on"] == waiting
    legacy = api.get(f"/v1/huddles/{huddle_id}").json()["huddle"]
    assert legacy["waiting_on"] == ["agent-a", "agent-b"]
    assert "votes" not in legacy
    other_dashboard = dashboard(api, "b")["huddles"][0]
    assert other_dashboard["waiting_on"] == waiting
    assert other_dashboard["dashboard_waiting_on"] == waiting


@pytest.mark.parametrize("endpoint", ["comments", "votes"])
@pytest.mark.parametrize("body", [b"", b"not json", b"[]"])
def test_huddle_actions_require_json_object(api, endpoint, body):
    huddle_id = start_huddle(api)
    assert api.post(f"/v1/huddles/{huddle_id}/{endpoint}", body=body, role="b").status == 400


def test_comments_support_general_and_referenced_sanitized_text(api):
    huddle_id = start_huddle(api)
    idea = post_idea(api, huddle_id)
    root = f"/v1/huddles/{huddle_id}"
    first = api.post(root + "/comments", {"text": "  More\x00 details\nplease  "}, "b")
    assert first.status == 201
    assert api.post(root + "/comments", {"text": "How about errors?", "ref_post_id": idea}, "b").status == 201
    current = api.get(root).json()["huddle"]
    assert current["waiting_on"] == []
    assert "votes" not in current
    assert [post["kind"] for post in current["posts"]] == ["idea", "comment", "comment"]
    assert current["posts"][1]["text"] == "More details\nplease"
    assert current["posts"][2]["ref_post_id"] == idea
    comments = [item for item in dashboard(api)["news"] if item["kind"] == "huddle_commented"]
    assert "More details\nplease" in comments[0]["summary"]


@pytest.mark.parametrize("payload", [
    {}, {"text": False}, {"text": "\x00"}, {"text": "x" * 8001},
    {"text": "😀" * 4001}, {"text": "\ud800"}, {"text": "ok", "ref_post_id": "bad"},
    {"text": "ok", "ref_post_id": MISSING_ID},
])
def test_comment_validation_is_atomic(api, payload):
    huddle_id = start_huddle(api)
    assert api.post(f"/v1/huddles/{huddle_id}/comments", payload, "b").status == 400
    assert api.get(f"/v1/huddles/{huddle_id}").json()["huddle"]["posts"] == []


def test_votes_upsert_without_posts_and_legacy_projection_changes(api):
    huddle_id = start_huddle(api)
    idea = post_idea(api, huddle_id)
    root = f"/v1/huddles/{huddle_id}"
    assert api.post(root + "/votes", {"ref_post_id": idea, "vote": "up", "note": "  Good\x00 idea "}, "b").status == 200
    snapshot = dashboard(api)["huddles"][0]
    assert snapshot["waiting_on"] == []
    assert len(snapshot["posts"]) == len(snapshot["votes"]) == 1
    assert snapshot["votes"][0]["note"] == "Good idea"
    assert api.post(root + "/votes", {"ref_post_id": idea, "vote": "down"}, "b").status == 200
    current = dashboard(api)["huddles"][0]
    assert len(current["votes"]) == 1
    assert current["votes"][0]["vote"] == "down"
    assert current["votes"][0]["note"] is None
    legacy = api.get(root).json()["huddle"]
    assert "votes" not in legacy
    assert legacy["waiting_on"] == ["agent-b"]
    assert len(legacy["posts"]) == 1
    other_dashboard = dashboard(api, "b")["huddles"][0]
    assert other_dashboard["votes"] == current["votes"]
    assert other_dashboard["waiting_on"] == []
    assert other_dashboard["dashboard_waiting_on"] == []
    vote_news = [item for item in dashboard(api)["news"] if item["kind"] == "huddle_voted"]
    assert "Backed idea" in vote_news[0]["summary"]
    assert "Good idea" in vote_news[0]["summary"]
    assert "Pushed back on idea" in vote_news[1]["summary"]


@pytest.mark.parametrize("extra", [
    {"vote": "agree"}, {"vote": None}, {"vote": True}, {"note": "x" * 501},
    {"note": "😀" * 251}, {"note": "\x00"}, {"note": "\ud800"}, {"note": 1},
    {"ref_post_id": "bad"}, {"ref_post_id": None}, {"ref_post_id": MISSING_ID},
])
def test_vote_validation_is_atomic(api, extra):
    huddle_id = start_huddle(api)
    idea = post_idea(api, huddle_id)
    body = {"ref_post_id": idea, "vote": "up", **extra}
    assert api.post(f"/v1/huddles/{huddle_id}/votes", body, "b").status == 400
    assert dashboard(api)["huddles"][0]["votes"] == []


def test_vote_rejects_own_idea_nonidea_and_cross_huddle_targets(api):
    huddle_id = start_huddle(api)
    idea = post_idea(api, huddle_id)
    root = f"/v1/huddles/{huddle_id}"
    assert api.post(root + "/votes", {"ref_post_id": idea, "vote": "up"}).status == 403
    comment = api.post(root + "/comments", {"text": "detail"}).json()["id"]
    assert api.post(root + "/votes", {"ref_post_id": comment, "vote": "up"}, "b").status == 400
    other = start_huddle(api)
    assert api.post(f"/v1/huddles/{other}/votes", {"ref_post_id": idea, "vote": "up"}, "b").status == 400
    assert api.post(f"/v1/huddles/{other}/comments", {"ref_post_id": idea, "text": "other"}, "b").status == 400


@pytest.mark.parametrize("path", [f"/v1/dashboard/tasks/{MISSING_ID}/take", f"/v1/huddles/{MISSING_ID}/comments", f"/v1/huddles/{MISSING_ID}/votes"])
def test_new_routes_retain_body_size_cap(api, path):
    assert api.post(path, body=b" " * (256 * 1024 + 1)).status == 413
