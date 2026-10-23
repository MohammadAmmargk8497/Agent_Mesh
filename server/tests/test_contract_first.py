import pytest


def create(api, **extra):
    result = api.post("/v1/tasks", {"title": "Task", "description": "Follow the contract", "assignee": "agent-b", **extra})
    assert result.status == 201, result.body
    return result.json()["id"]


def finish(api, task_id):
    assert api.post(f"/v1/tasks/{task_id}/claim", {}, "b").status == 200
    assert api.post(f"/v1/tasks/{task_id}/complete", {"summary": "Published"}, "b").status == 200


def lock_master(api):
    assert api.post("/v1/master/sections", {"section": "Goal", "text": "Build the service"}).status == 201
    assert api.post("/v1/master/submit", {}).status == 200
    assert api.post("/v1/master/vote", {"decision": "approve"}, "b").status == 200


@pytest.mark.parametrize("marker", ["contract", "skeleton"])
def test_explicit_board_contract_can_appear_after_task_one(api, marker):
    lock_master(api)
    result = api.post("/v1/tasks/propose", {"tasks": [
        {"title": "Implementation", "description": "Use the interface", "assignee": "agent-b"},
        {"title": "Interface", "description": "Publish the skeleton", "assignee": "agent-b", marker: True},
        {"title": "Tests", "description": "Verify the interface", "assignee": "agent-b"},
    ]})
    assert result.status == 201, result.body
    tasks = result.json()["tasks"]
    assert [task["title"] for task in tasks] == ["Implementation", "Interface", "Tests"]
    assert tasks[1]["contract"] is True
    assert tasks[1]["depends_on"] == []
    for task in (tasks[0], tasks[2]):
        assert task["depends_on"] == task["waiting_on"] == [tasks[1]["id"]]
        blocked = api.post(f"/v1/tasks/{task['id']}/claim", {}, "b")
        assert blocked.status == 409
        assert blocked.json()["waiting_on"] == [tasks[1]["id"]]
    finish(api, tasks[1]["id"])
    assert api.post(f"/v1/tasks/{tasks[0]['id']}/claim", {}, "b").status == 200


def test_board_without_explicit_contract_uses_first_and_retains_external_dependencies(api):
    external = create(api)
    lock_master(api)
    result = api.post("/v1/tasks/propose", {"tasks": [
        {"title": "First", "description": "Skeleton", "assignee": "agent-b"},
        {"title": "Second", "description": "Implementation", "assignee": "agent-b", "depends_on": [external]},
    ]})
    assert result.status == 201, result.body
    root, child = result.json()["tasks"]
    assert root["contract"] is True
    assert child["depends_on"] == [external, root["id"]]
    finish(api, root["id"])
    assert api.post(f"/v1/tasks/{child['id']}/claim", {}, "b").status == 409
    finish(api, external)
    assert api.post(f"/v1/tasks/{child['id']}/claim", {}, "b").status == 200


def test_done_direct_dependency_cannot_bypass_reopened_contract_ancestor(api):
    contract = create(api, skeleton=True)
    middle = create(api, depends_on=[contract])
    finish(api, contract)
    finish(api, middle)
    leaf = create(api, depends_on=[middle])
    assert api.post(f"/v1/tasks/{contract}/review", {"decision": "reopen"}).status == 200
    task = api.get(f"/v1/tasks/{leaf}", "b").json()["task"]
    assert task["depends_on"] == [middle]
    assert task["waiting_on"] == [contract]
    blocked = api.post(f"/v1/tasks/{leaf}/claim", {}, "b")
    assert blocked.status == 409
    assert blocked.json() == {"error": "waiting on dependencies", "waiting_on": [contract]}
    finish(api, contract)
    events = api.get("/v1/events?after=0", "b").json()["events"]
    assert any(event["type"] == "task_unblocked" and event["ref"] == leaf for event in events)
    assert api.post(f"/v1/tasks/{leaf}/claim", {}, "b").status == 200


def test_task_without_dependencies_is_not_blocked_by_an_unrelated_contract(api):
    create(api, contract=True)
    independent = create(api)
    assert api.post(f"/v1/tasks/{independent}/claim", {}, "b").status == 200


def test_huddle_tasks_keep_supplied_dependencies_and_contract_marker(api):
    contract = create(api, contract=True)
    huddle = api.post("/v1/huddles", {"kind": "design", "topic": "Plan implementation"}).json()["id"]
    result = api.post(f"/v1/huddles/{huddle}/close", {"decision": "Use the skeleton", "tasks": [
        {"title": "Implementation", "description": "Follow the skeleton", "assignee": "agent-b", "depends_on": [contract], "skeleton": True},
    ]})
    assert result.status == 200, result.body
    child = result.json()["tasks"][0]
    task = api.get(f"/v1/tasks/{child}", "b").json()["task"]
    assert task["contract"] is True
    assert task["depends_on"] == task["waiting_on"] == [contract]
    assert api.post(f"/v1/tasks/{child}/claim", {}, "b").status == 409
    finish(api, contract)
    assert api.post(f"/v1/tasks/{child}/claim", {}, "b").status == 200


@pytest.mark.parametrize("marker", [None, 1, "true"])
def test_skeleton_marker_requires_boolean_and_invalid_board_is_atomic(api, marker):
    assert api.post("/v1/tasks", {"title": "Task", "description": "Description", "assignee": "agent-b", "skeleton": marker}).status == 400
    lock_master(api)
    assert api.post("/v1/tasks/propose", {"tasks": [
        {"title": "Valid", "description": "Description", "assignee": "agent-b"},
        {"title": "Invalid", "description": "Description", "assignee": "agent-b", "skeleton": marker},
    ]}).status == 400
    assert api.get("/v1/tasks").json()["tasks"] == []
