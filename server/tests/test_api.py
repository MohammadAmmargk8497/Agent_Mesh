import http.client
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import wait_until


def test_health_identity_and_security_headers(api):
    health = api.get("/health", None)
    assert health.status == 200
    assert health.json() == {"ok": True, "service": "agentmesh"}
    for role, identity in (("a", "agent-a"), ("b", "agent-b")):
        result = api.get("/v1/whoami", role)
        assert result.status == 200
        assert result.json() == {"agent": identity}
        assert result.headers["content-type"] == "application/json; charset=utf-8"
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert int(result.headers["content-length"]) == len(result.body)
        assert "Python" not in result.headers.get("server", "")


@pytest.mark.parametrize("authorization", [None, "", "Basic wrong", "bearer wrong", "Bearer wrong", "Bearer"])
def test_malformed_auth_and_forged_agent_cannot_authenticate(api, authorization):
    headers = {"x-agentmesh-agent": "agent-a"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = api.get("/v1/whoami", None, headers=headers)
    assert response.status == 401
    assert response.json() == {"error": "unauthorized"}


def test_authenticated_identity_ignores_forged_agent_header(api):
    assert api.get("/v1/whoami", "b", headers={"x-agentmesh-agent": "agent-a"}).json() == {"agent": "agent-b"}


def test_messages_are_recipient_scoped_and_ack_is_idempotent(api):
    message = api.post("/v1/messages", {"to": "agent-b", "text": "hello"})
    assert message.status == 201
    message_id = message.json()["id"]
    uuid.UUID(message_id)
    assert api.get("/v1/messages").json() == {"messages": []}
    received = api.get("/v1/messages?unread=true", "b").json()["messages"]
    assert len(received) == 1
    assert received[0] == {
        "id": message_id, "sender": "agent-a", "recipient": "agent-b", "text": "hello",
        "task_id": None, "created_at": message.json()["created_at"], "acked_at": None,
    }
    assert api.post(f"/v1/messages/{message_id}/ack", {}).json() == {"ok": True, "changed": 0}
    assert api.post(f"/v1/messages/{message_id}/ack", {}, "b").json() == {"ok": True, "changed": 1}
    assert api.post(f"/v1/messages/{message_id}/ack", {}, "b").json() == {"ok": True, "changed": 0}
    assert api.get("/v1/messages?unread=true", "b").json() == {"messages": []}
    assert len(api.get("/v1/messages", "b").json()["messages"]) == 1
    reverse = api.post("/v1/messages", {"to": "agent-a", "text": "reply"}, "b")
    assert reverse.status == 201
    assert [m["text"] for m in api.get("/v1/messages").json()["messages"]] == ["reply"]
    assert [m["text"] for m in api.get("/v1/messages", "b").json()["messages"]] == ["hello"]


def test_task_lifecycle_review_notes_and_inbox(api):
    task_id = api.task()
    root = f"/v1/tasks/{task_id}"
    task = api.get(root, "b").json()["task"]
    assert task["title"] == "test task"
    assert task["description"] == "test description"
    assert task["acceptance_criteria"] == ["covered by tests"]
    assert task["result"] is None
    inbox = api.get("/v1/inbox", "b").json()
    assert inbox["agent"] == "agent-b"
    assert [t["id"] for t in inbox["assigned_tasks"]] == [task_id]
    assert api.post(root + "/complete", {"summary": "too early"}, "b").status == 409
    assert api.post(root + "/claim", {}).status == 403
    assert api.post(root + "/claim", {}, "b").json() == {"id": task_id, "status": "in_progress"}
    assert api.post(root + "/claim", {}, "b").status == 409
    assert api.post(root + "/complete", {"summary": "paused", "status": "blocked"}, "b").json()["status"] == "blocked"
    assert [t["id"] for t in api.get("/v1/inbox").json()["tasks_to_review"]] == [task_id]
    assert api.post(root + "/review", {"decision": "accept"}, "b").status == 403
    assert api.post(root + "/review", {"decision": "reopen", "note": "add coverage"}).json()["status"] == "open"
    notes = api.get("/v1/inbox", "b").json()["unread_messages"]
    assert notes[0]["text"] == "[review: reopen] add coverage"
    assert notes[0]["task_id"] == task_id
    assert api.post(root + "/claim", {}, "b").status == 200
    assert api.post(root + "/complete", {"summary": "finished"}, "b").json()["status"] == "done"
    assert api.post(root + "/review", {"decision": "accept"}).json()["status"] == "accepted"
    assert api.post(root + "/cancel", {}).status == 409
    assert [t["id"] for t in api.get("/v1/tasks?status=accepted").json()["tasks"]] == [task_id]


def test_task_cancel_and_task_privacy(api):
    private_id = api.task(assignee="agent-a")
    assert api.get(f"/v1/tasks/{private_id}", "b").status == 404
    assert api.get("/v1/tasks", "b").json() == {"tasks": []}
    shared_id = api.task()
    assert api.post(f"/v1/tasks/{shared_id}/cancel", {}, "b").status == 403
    assert api.post(f"/v1/tasks/{shared_id}/cancel", {}).json() == {"id": shared_id, "status": "cancelled"}
    assert api.post(f"/v1/tasks/{shared_id}/cancel", {}).status == 409
    assert api.request("PUT", f"/v1/tasks/{shared_id}").status == 405
    assert api.get(f"/v1/tasks/{uuid.uuid4()}").status == 404


def test_patches_metadata_full_text_and_audit(api):
    patch = api.post("/v1/patches", {"title": "diff", "text": "--- a\n+++ b\n"})
    assert patch.status == 201
    patch_id = patch.json()["id"]
    listed = api.get("/v1/patches", "b").json()["patches"]
    assert listed == [{"id": patch_id, "sender": "agent-a", "title": "diff", "task_id": None,
                       "created_at": patch.json()["created_at"], "size": len("--- a\n+++ b\n")}]
    assert api.get(f"/v1/patches/{patch_id}", "b").json()["patch"]["text"] == "--- a\n+++ b\n"
    assert api.get(f"/v1/patches/{uuid.uuid4()}").status == 404
    sent = api.post("/v1/messages", {"to": "agent-b", "text": "audited"}).json()["id"]
    audit = api.get("/v1/transcript", "viewer").json()["audit"]
    assert [{"actor": row["actor"], "action": row["action"], "ref": row["ref"]} for row in audit] == [
        {"actor": "agent-a", "action": "message.send", "ref": sent},
    ]


def test_viewer_transcript_is_read_only_and_requires_viewer_token(api):
    api.task()
    assert api.get("/v1/transcript", None).status == 401
    assert api.get("/v1/transcript").status == 403
    transcript = api.get("/v1/transcript", "viewer")
    assert transcript.status == 200
    assert {"messages", "tasks", "patches", "audit", "events", "last_event_id"} <= set(transcript.json())
    assert transcript.json()["tasks"][0]["acceptance_criteria"] == ["covered by tests"]
    for method, path in (("GET", "/health"), ("GET", "/v1/whoami"), ("GET", "/v1/inbox"),
                         ("GET", "/v1/events"), ("GET", "/v1/patches"), ("POST", "/v1/messages"),
                         ("POST", "/v1/transcript"), ("GET", "/v1/transcript/")):
        assert api.request(method, path, "viewer", data={}).status == 403


@pytest.mark.parametrize("after", ["abc", "1.5", "", "1e3", "9007199254740992"])
def test_viewer_after_validation(api, after):
    assert api.get("/v1/transcript?after=" + after, "viewer").status == 400


def test_viewer_transcript_event_cursor(api):
    api.post("/v1/messages", {"to": "agent-b", "text": "first"})
    first = api.get("/v1/transcript", "viewer").json()
    assert first["events"][-1]["type"] == "message"
    cursor = first["last_event_id"]
    empty = api.get(f"/v1/transcript?after={cursor}", "viewer").json()
    assert empty["events"] == [] and empty["last_event_id"] == cursor
    api.post("/v1/messages", {"to": "agent-a", "text": "second"}, "b")
    later = api.get(f"/v1/transcript?after={cursor}", "viewer").json()
    assert len(later["events"]) == 1
    assert later["events"][0]["actor"] == "agent-b"


@pytest.mark.parametrize("path,body", [
    ("/v1/tasks", b"[]"), ("/v1/tasks", b"null"), ("/v1/tasks", b"true"),
    ("/v1/tasks", b"\"string\""), ("/v1/tasks", b"{bad"), ("/v1/tasks", b""),
])
def test_body_rejects_invalid_json_or_non_objects(api, path, body):
    result = api.post(path, body=body, headers={"Content-Type": "application/json"})
    assert result.status == 400


@pytest.mark.parametrize("path,data", [
    ("/v1/tasks", {"title": " ", "description": "x", "assignee": "agent-b"}),
    ("/v1/tasks", {"title": "x" * 201, "description": "x", "assignee": "agent-b"}),
    ("/v1/tasks", {"title": "x", "description": "x" * 8001, "assignee": "agent-b"}),
    ("/v1/tasks", {"title": "x", "description": "x", "assignee": "viewer"}),
    ("/v1/tasks", {"title": "x", "description": "x", "assignee": "agent-b", "acceptance_criteria": ["x"] * 21}),
    ("/v1/tasks", {"title": "x", "description": "x", "assignee": "agent-b", "acceptance_criteria": [False]}),
    ("/v1/messages", {"to": "agent-a", "text": "self"}),
    ("/v1/messages", {"to": "viewer", "text": "wrong recipient"}),
    ("/v1/messages", {"to": "agent-b", "text": ""}),
    ("/v1/messages", {"to": "agent-b", "text": "x", "task_id": "../patches"}),
    ("/v1/patches", {"title": "x", "text": "x", "task_id": False}),
    ("/v1/patches", {"title": "x", "text": "x" * (200 * 1024 + 1)}),
])
def test_validation_matches_worker(api, path, data):
    assert api.post(path, data).status == 400


def test_nullable_criteria_and_optional_task_id(api):
    assert api.post("/v1/tasks", {"title": "x", "description": "x", "assignee": "agent-b", "acceptance_criteria": None}).status == 201
    for optional in (None, ""):
        assert api.post("/v1/messages", {"to": "agent-b", "text": "x", "task_id": optional}).status == 201


def test_task_action_validation(api):
    root = f"/v1/tasks/{api.task()}"
    assert api.post(root + "/claim", {}, "b").status == 200
    for body in ({"summary": ""}, {"summary": "x", "status": None}, {"summary": "x", "status": "bogus"}):
        assert api.post(root + "/complete", body, "b").status == 400
    assert api.post(root + "/complete", {"summary": "done"}, "b").status == 200
    for body in ({"decision": "invalid"}, {"decision": "accept", "note": None}, {"decision": "accept", "note": ""}):
        assert api.post(root + "/review", body).status == 400


def test_events_are_recipient_scoped_and_trailing_slash_is_supported(api):
    sent = api.post("/v1/messages", {"to": "agent-b", "text": "hello"}).json()["id"]
    assert api.get("/v1/events?after=0").json()["events"] == []
    events = api.get("/v1/events?after=0", "b").json()["events"]
    assert len(events) == 1 and events[0]["ref"] == sent
    assert api.get("/v1/whoami///").json() == {"agent": "agent-a"}
    for query in ("after=abc", "after=0.5", "wait=bad"):
        assert api.get("/v1/events?" + query).status == 400


def test_long_poll_concurrent_waiters_both_wake_within_one_second(api):
    with ThreadPoolExecutor(max_workers=2) as executor:
        polls = [executor.submit(api.get, "/v1/events?after=0&wait=5", "b", timeout=7) for _ in range(2)]
        wait_until(lambda: api.server.state.waiters.get("agent-b", 0) == 2)
        start = time.monotonic()
        message = api.post("/v1/messages", {"to": "agent-b", "text": "wake both"}).json()["id"]
        results = [future.result(timeout=1) for future in polls]
        assert time.monotonic() - start < 1
        for result in results:
            assert result.status == 200
            assert result.json()["events"][0]["ref"] == message


def test_long_poll_fifth_concurrent_waiter_is_rate_limited(api):
    with ThreadPoolExecutor(max_workers=4) as executor:
        polls = [executor.submit(api.get, "/v1/events?after=0&wait=5", "b", timeout=7) for _ in range(4)]
        wait_until(lambda: api.server.state.waiters.get("agent-b", 0) == 4)
        try:
            assert api.get("/v1/events?after=0&wait=5", "b").status == 429
        finally:
            api.post("/v1/messages", {"to": "agent-b", "text": "finish waiting"})
            for poll in polls:
                assert poll.result(timeout=1).status == 200


def test_concurrent_claim_is_atomic(api):
    path = f"/v1/tasks/{api.task()}/claim"
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = [executor.submit(api.post, path, {}, "b") for _ in range(2)]
        assert sorted(future.result().status for future in claims) == [200, 409]


def test_long_poll_timeout(api):
    start = time.monotonic()
    result = api.get("/v1/events?after=0&wait=0.05", "b")
    assert result.status == 200
    assert result.json() == {"events": [], "last_event_id": 0}
    assert 0.03 < time.monotonic() - start < 1


def test_body_cap_rejects_oversized_header_and_content(api):
    assert api.post("/v1/patches", {"title": "large", "text": "x" * (256 * 1024)}).status == 413
    connection = http.client.HTTPConnection(api.host, api.port, timeout=3)
    try:
        connection.putrequest("POST", "/v1/messages")
        connection.putheader("Authorization", "Bearer " + api.tokens["a"])
        connection.putheader("Content-Length", str(256 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        response.read()
    finally:
        connection.close()


def test_body_cap_rejects_oversized_chunked_content(api):
    connection = http.client.HTTPConnection(api.host, api.port, timeout=3)
    try:
        connection.request("POST", "/v1/messages", body=iter([b"x" * (256 * 1024 + 1)]),
                           headers={"Authorization": "Bearer " + api.tokens["a"]}, encode_chunked=True)
        response = connection.getresponse()
        assert response.status == 413
        response.read()
    finally:
        connection.close()


def test_http11_keepalive_responses_are_framed(api):
    connection = http.client.HTTPConnection(api.host, api.port, timeout=3)
    try:
        for path in ("/health", "/health"):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.version == 11
            assert response.status == 200
            assert int(response.getheader("Content-Length")) == len(response.read())
    finally:
        connection.close()


def test_websocket_connect_is_worker_only(api):
    assert api.get("/v1/connect", headers={"Upgrade": "websocket", "Connection": "Upgrade"}).status == 404
