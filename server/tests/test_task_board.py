import uuid

import pytest


def create(api, *, role="a", **extra):
    result = api.post("/v1/tasks", {
        "title": "Implement", "description": "Follow the contract", "assignee": "agent-b", **extra,
    }, role)
    assert result.status == 201, result.body
    return result.json()["id"]


def task(api, task_id, role="a"):
    return api.get(f"/v1/tasks/{task_id}", role).json()["task"]


def lock_master(api):
    assert api.post("/v1/master/sections", {"section": "Goal", "text": "Build a service"}).status in (200, 201)
    assert api.post("/v1/master/submit", {}).status == 200
    assert api.post("/v1/master/vote", {"decision": "approve"}, "b").status == 200


def finish(api, task_id, role="b"):
    root = f"/v1/tasks/{task_id}"
    assert api.post(root + "/claim", {}, role).status == 200
    assert api.post(root + "/complete", {"summary": "Implemented"}, role).status == 200


def test_task_board_metadata_defaults_and_cleaned_file_areas(api):
    old_style = task(api, create(api))
    assert old_style["owner"] == "agent-b"
    assert old_style["file_areas"] == old_style["depends_on"] == old_style["waiting_on"] == []
    assert old_style["contract"] is False
    root = create(api, owner="agent-a", file_areas=[" src/\x00api.py ", "tests/**"], contract=True)
    board = task(api, root)
    assert board["owner"] == "agent-a"
    assert board["file_areas"] == ["src/api.py", "tests/**"]
    assert board["contract"] is True
    listed = api.get("/v1/tasks", "b").json()["tasks"]
    assert next(row for row in listed if row["id"] == root)["file_areas"] == board["file_areas"]


def test_task_dependencies_wait_for_all_done_or_accepted(api):
    one, two = create(api, contract=True), create(api)
    dependent = create(api, depends_on=[one, two])
    assert task(api, dependent)["waiting_on"] == [one, two]
    blocked = api.post(f"/v1/tasks/{dependent}/claim", {}, "b")
    assert blocked.status == 409
    assert blocked.json()["error"] == "waiting on dependencies"
    finish(api, one)
    assert task(api, dependent)["waiting_on"] == [two]
    assert api.post(f"/v1/tasks/{one}/review", {"decision": "accept"}).status == 200
    assert api.post(f"/v1/tasks/{dependent}/claim", {}, "b").status == 409
    finish(api, two)
    assert task(api, dependent)["waiting_on"] == []
    assert api.post(f"/v1/tasks/{dependent}/claim", {}, "b").json() == {"id": dependent, "status": "in_progress"}
    events = api.get("/v1/events?after=0", "b").json()["events"]
    assert any(row["type"] == "task_unblocked" and row["ref"] == dependent for row in events)
    news = api.get("/v1/news?peek=true", "b").json()["news"]
    assert any(row["kind"] == "task_unblocked" and row["ref"] == dependent for row in news)


def test_task_unblocked_notification_targets_explicit_owner(api):
    skeleton = create(api)
    dependent = create(api, owner="agent-a", depends_on=[skeleton])
    finish(api, skeleton)
    events = api.get("/v1/events?after=0").json()["events"]
    assert any(row["type"] == "task_unblocked" and row["ref"] == dependent for row in events)


def test_accepting_done_dependency_does_not_repeat_unblock_notification(api):
    skeleton = create(api)
    dependent = create(api, depends_on=[skeleton])
    finish(api, skeleton)
    before = api.get("/v1/events?after=0", "b").json()["events"]
    unblock = [row for row in before if row["type"] == "task_unblocked" and row["ref"] == dependent]
    assert len(unblock) == 1
    assert api.post(f"/v1/tasks/{skeleton}/review", {"decision": "accept"}).status == 200
    after = api.get("/v1/events?after=0", "b").json()["events"]
    assert [row for row in after if row["type"] == "task_unblocked" and row["ref"] == dependent] == unblock
    news = api.get("/v1/news?peek=true", "b").json()["news"]
    assert len([row for row in news if row["kind"] == "task_unblocked" and row["ref"] == dependent]) == 1


def test_accepting_blocked_dependency_unblocks_and_notifies_default_owner(api):
    skeleton = create(api)
    dependent = create(api, depends_on=[skeleton])
    assert api.post(f"/v1/tasks/{skeleton}/claim", {}, "b").status == 200
    assert api.post(f"/v1/tasks/{skeleton}/complete", {"summary": "Needs review", "status": "blocked"}, "b").status == 200
    assert task(api, dependent)["waiting_on"] == [skeleton]
    before = api.get("/v1/events?after=0", "b").json()["events"]
    assert not any(row["type"] == "task_unblocked" and row["ref"] == dependent for row in before)
    assert api.post(f"/v1/tasks/{skeleton}/review", {"decision": "accept"}).status == 200
    assert task(api, dependent)["waiting_on"] == []
    after = api.get("/v1/events?after=0", "b").json()["events"]
    assert len([row for row in after if row["type"] == "task_unblocked" and row["ref"] == dependent]) == 1
    assert api.post(f"/v1/tasks/{dependent}/claim", {}, "b").status == 200


def test_cancelled_or_blocked_dependency_does_not_unblock(api):
    cancelled = create(api)
    assert api.post(f"/v1/tasks/{cancelled}/cancel", {}).status == 200
    dependent = create(api, depends_on=[cancelled])
    assert task(api, dependent)["waiting_on"] == [cancelled]
    assert api.post(f"/v1/tasks/{dependent}/claim", {}, "b").status == 409
    blocked = create(api)
    assert api.post(f"/v1/tasks/{blocked}/claim", {}, "b").status == 200
    assert api.post(f"/v1/tasks/{blocked}/complete", {"summary": "Need input", "status": "blocked"}, "b").status == 200
    other = create(api, depends_on=[blocked])
    assert api.post(f"/v1/tasks/{other}/claim", {}, "b").status == 409


def test_task_dependencies_must_exist_and_be_visible_to_creator(api):
    private = create(api, assignee="agent-a")
    for dependency in (str(uuid.uuid4()), private):
        assert api.post("/v1/tasks", {
            "title": "private dependency", "description": "not visible", "assignee": "agent-a",
            "depends_on": [dependency],
        }, "b").status == 400


@pytest.mark.parametrize("extra", [
    {"owner": "viewer"}, {"contract": 1}, {"contract": "true"},
    {"file_areas": "src/**"}, {"file_areas": [False]}, {"file_areas": ["x"] * 21},
    {"file_areas": ["x" * 501]}, {"file_areas": ["😀" * 251]}, {"file_areas": ["\x00"]},
    {"depends_on": "not-a-list"}, {"depends_on": ["not-a-uuid"]},
    {"depends_on": [str(uuid.uuid4())] * 21},
])
def test_task_board_metadata_validation(api, extra):
    assert api.post("/v1/tasks", {"title": "t", "description": "d", "assignee": "agent-b", **extra}).status == 400


def test_lead_proposes_contract_first_tasks_only_for_locked_prompt(api):
    proposal = {"tasks": [
        {"title": "Contract", "description": "Publish skeleton", "assignee": "agent-a", "file_areas": ["api/**"]},
        {"title": "Implementation", "description": "Use skeleton", "assignee": "agent-b", "acceptance_criteria": ["works"]},
        {"title": "Tests", "description": "Cover skeleton", "assignee": "agent-b"},
    ]}
    assert api.post("/v1/master/sections", {"section": "Goal", "text": "Build a service"}).status in (200, 201)
    assert api.post("/v1/tasks/propose", proposal).status == 409
    assert api.post("/v1/tasks/propose", proposal, "b").status == 403
    api.post("/v1/master/submit", {})
    api.post("/v1/master/vote", {"decision": "approve"}, "b")
    assert api.post("/v1/tasks/propose", proposal, "b").status == 403
    result = api.post("/v1/tasks/propose", proposal)
    assert result.status == 201, result.body
    created = result.json()["tasks"]
    assert len(created) == 3
    assert [row["owner"] for row in created] == ["agent-a", "agent-b", "agent-b"]
    assert created[0]["contract"] is True
    assert created[0]["depends_on"] == []
    assert created[0]["file_areas"] == ["api/**"]
    assert all(row["depends_on"] == [created[0]["id"]] for row in created[1:])
    assert all(row["waiting_on"] == [created[0]["id"]] for row in created[1:])
    assert api.post(f"/v1/tasks/{created[1]['id']}/claim", {}, "b").status == 409
    finish(api, created[0]["id"], "a")
    events = api.get("/v1/events?after=0", "b").json()["events"]
    assert {row["ref"] for row in events if row["type"] == "task_unblocked"} == {row["id"] for row in created[1:]}
    assert api.post(f"/v1/tasks/{created[1]['id']}/claim", {}, "b").status == 200


@pytest.mark.parametrize("tasks", [[], [{}] * 21, [{"title": "t", "description": "d", "assignee": "viewer"}]])
def test_task_proposal_validation_is_atomic(api, tasks):
    lock_master(api)
    assert api.post("/v1/tasks/propose", {"tasks": tasks}).status == 400
    assert api.get("/v1/tasks").json()["tasks"] == []
