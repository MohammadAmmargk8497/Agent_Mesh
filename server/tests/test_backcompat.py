import uuid

import pytest

NEW_ROUTES = [
    ("GET", "/v1/master"), ("POST", "/v1/master/sections"), ("POST", "/v1/master/submit"),
    ("POST", "/v1/master/vote"), ("POST", "/v1/tasks/propose"),
    ("GET", "/v1/claims"), ("POST", "/v1/claims"), ("POST", "/v1/claims/release"),
    ("GET", "/v1/huddles"), ("POST", "/v1/huddles"),
    ("GET", f"/v1/huddles/{uuid.UUID(int=1)}"),
    ("POST", f"/v1/huddles/{uuid.UUID(int=1)}/posts"),
    ("POST", f"/v1/huddles/{uuid.UUID(int=1)}/close"),
    ("GET", "/v1/questions"), ("POST", "/v1/questions"),
    ("POST", f"/v1/questions/{uuid.UUID(int=1)}/answer"),
    ("GET", "/v1/news"), ("POST", "/v1/commits"),
]


@pytest.mark.parametrize("method,path", NEW_ROUTES)
def test_coordination_routes_reject_removed_viewer_credentials(api, method, path):
    assert api.request(method, path, None, data={}).status == 401
    assert api.request(method, path, "viewer", data={}).status == 401


def test_old_task_actions_and_identity_keep_exact_response_shapes(api):
    assert api.get("/v1/whoami").json() == {"agent": "agent-a"}
    created = api.post("/v1/tasks", {"title": "Old client", "description": "No coordination fields", "assignee": "agent-b"})
    assert created.status == 201
    assert created.json() == {"id": created.json()["id"], "status": "open", "created_by": "agent-a"}
    task_id = created.json()["id"]
    assert api.post(f"/v1/tasks/{task_id}/claim", {}, "b").json() == {"id": task_id, "status": "in_progress"}
    assert api.post(f"/v1/tasks/{task_id}/complete", {"summary": "done"}, "b").json() == {"id": task_id, "status": "done"}
    assert api.post(f"/v1/tasks/{task_id}/review", {"decision": "accept"}).json() == {"id": task_id, "status": "accepted"}
    cancelled = api.task()
    assert api.post(f"/v1/tasks/{cancelled}/cancel", {}).json() == {"id": cancelled, "status": "cancelled"}
    assert set(api.get("/v1/inbox").json()) == {"agent", "unread_messages", "assigned_tasks", "tasks_to_review", "last_event_id"}
    events = api.get("/v1/events?after=0", "b").json()
    assert set(events) == {"events", "last_event_id"}
    assert all(set(row) == {"id", "recipient", "type", "ref", "actor", "created_at"} for row in events["events"])


def test_old_message_and_patch_endpoints_remain_unchanged(api):
    sent = api.post("/v1/messages", {"to": "agent-b", "text": "  old\x00message  "})
    assert sent.status == 201
    assert set(sent.json()) == {"id", "created_at"}
    message_id = sent.json()["id"]
    messages = api.get("/v1/messages?unread=true", "b").json()
    assert messages == {"messages": [{
        "id": message_id, "sender": "agent-a", "recipient": "agent-b", "text": "  old\x00message  ",
        "task_id": None, "created_at": sent.json()["created_at"], "acked_at": None,
    }]}
    assert api.post(f"/v1/messages/{message_id}/ack", {}, "b").json() == {"ok": True, "changed": 1}
    patch = api.post("/v1/patches", {"title": "  patch  ", "text": "--- a\n+++ b\n"})
    assert patch.status == 201
    assert set(patch.json()) == {"id", "created_at"}
    patch_id = patch.json()["id"]
    assert api.get("/v1/patches", "b").json() == {"patches": [{
        "id": patch_id, "sender": "agent-a", "title": "  patch  ", "task_id": None,
        "created_at": patch.json()["created_at"], "size": 12,
    }]}
    assert api.get(f"/v1/patches/{patch_id}", "b").json() == {"patch": {
        "id": patch_id, "sender": "agent-a", "title": "  patch  ", "text": "--- a\n+++ b\n",
        "task_id": None, "created_at": patch.json()["created_at"],
    }}


def test_agent_dashboard_adds_coordination_without_removing_old_keys(api):
    api.task()
    api.post("/v1/master/sections", {"section": "Goal", "text": "Shared project"})
    api.post("/v1/claims", {"path": "src/api.py"})
    huddle_id = api.post("/v1/huddles", {"kind": "standup", "topic": "Today"}).json()["id"]
    api.post(f"/v1/huddles/{huddle_id}/posts", {"kind": "idea", "text": "Publish skeleton"}, "b")
    api.post("/v1/questions", {"to": "agent-b", "text": "Ready?"})
    result = api.get("/v1/dashboard/state")
    assert result.status == 200
    transcript = result.json()
    assert {"messages", "tasks", "patches", "audit", "events", "last_event_id"} <= set(transcript)
    assert {"master_prompt", "huddles", "claims", "questions", "news"} <= set(transcript)
    assert transcript["master_prompt"]["sections"][0]["text"] == "Shared project"
    assert transcript["huddles"][0]["posts"][0]["text"] == "Publish skeleton"
    assert transcript["claims"][0]["path"] == "src/api.py"
    assert transcript["questions"][0]["text"] == "Ready?"
    assert transcript["news"]
    assert len(transcript["tasks"]) == 1
