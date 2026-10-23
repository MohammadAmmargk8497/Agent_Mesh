import pytest


@pytest.mark.parametrize("value", ["1_0", "inf", "infinity", "0x1_0", "%1C0"])
def test_events_reject_python_only_number_syntax(api, value):
    assert api.get("/v1/events?wait=" + value).status == 400


@pytest.mark.parametrize("value", ["0x0", "0b0", "0o0", "%EF%BB%BF0%EF%BB%BF"])
def test_events_accept_worker_number_syntax(api, value):
    response = api.get("/v1/events?after=" + value)
    assert response.status == 200
    assert response.json() == {"events": [], "last_event_id": 0}


def test_viewer_accepts_leading_zero_cursor_like_worker(api):
    response = api.get("/v1/transcript?after=" + "0" * 200, "viewer")
    assert response.status == 200
    assert response.json()["last_event_id"] == 0


def test_task_trims_javascript_whitespace(api):
    response = api.post("/v1/tasks", {
        "title": "\ufeff task \ufeff", "description": "\ufeff description \ufeff", "assignee": "agent-b",
    })
    assert response.status == 201
    task = api.get("/v1/tasks/" + response.json()["id"]).json()["task"]
    assert task["title"] == "task"
    assert task["description"] == "description"
    assert api.post("/v1/tasks", {
        "title": "\ufeff", "description": "description", "assignee": "agent-b",
    }).status == 400


@pytest.mark.parametrize("body,expected", [
    (b'\xef\xbb\xbf{"to":"agent-b","text":"BOM"}', "BOM"),
    (b'{"to":"agent-b","text":"\xff"}', "\ufffd"),
])
def test_json_decoding_matches_worker_textdecoder(api, body, expected):
    assert api.post("/v1/messages", body=body).status == 201
    assert api.get("/v1/messages", "b").json()["messages"][0]["text"] == expected
