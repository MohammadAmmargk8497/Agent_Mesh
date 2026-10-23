from urllib.parse import quote

import pytest
from agentmesh_server.state import now_ms


def claim(api, path="src/service.py", role="a", **extra):
    response = api.post("/v1/claims", {"path": path, **extra}, role)
    assert response.status == 201, response.body
    return response.json()


def test_claim_conflict_renewal_check_and_release(api):
    task_id = api.task()
    before = now_ms()
    held = claim(api, "./src//service.py", task_id=task_id)
    after = now_ms()
    assert held["path"] == "src/service.py"
    assert held["agent"] == "agent-a"
    assert held["task_id"] == task_id
    assert before + 30 * 60_000 <= held["expires_at"] <= after + 30 * 60_000
    listed = api.get("/v1/claims", "b").json()["claims"]
    assert listed == [held]
    checked = api.get("/v1/claims?path=" + quote("./src/service.py", safe=""), "b").json()["claims"]
    assert checked == [held]
    conflict = api.post("/v1/claims", {"path": "src/service.py"}, "b")
    assert conflict.status == 409
    assert conflict.json()["held_by"] == "agent-a"
    assert conflict.json()["expires_at"] == held["expires_at"]
    renewed = claim(api, "src/service.py", task_id=task_id)
    assert renewed["expires_at"] >= held["expires_at"]
    assert api.post("/v1/claims/release", {"path": "src/service.py"}, "b").status == 403
    assert api.post("/v1/claims/release", {"path": "src/service.py"}).status == 200
    assert api.get("/v1/claims?path=src/service.py", "b").json()["claims"] == []
    assert claim(api, "src/service.py", "b")["agent"] == "agent-b"


def test_expired_claims_are_hidden_and_can_be_taken_without_waiting_for_sweep(api):
    claim(api)
    state = api.server.state
    with state.lock:
        state.db.execute("UPDATE file_claims SET expires_at=?", (now_ms() - 1,))
    assert api.get("/v1/claims").json()["claims"] == []
    assert claim(api, role="b")["agent"] == "agent-b"
    with state.lock:
        state.db.execute("UPDATE file_claims SET expires_at=?", (now_ms() - 1,))
    state.sweep()
    with state.lock:
        assert state.rows("SELECT * FROM file_claims") == []


@pytest.mark.parametrize("path", [
    "", " ", "/etc/passwd", "../service.py", "src/../service.py", "..", ".",
    "C:\\work\\service.py", "\\\\host\\share", "src/\x00service.py", "src/\nservice.py", "src/\x85service.py",
    "x" * 501, "😀" * 251, False,
])
def test_file_claims_reject_unsafe_or_oversized_paths(api, path):
    assert api.post("/v1/claims", {"path": path}).status == 400


def test_claims_allow_distinct_files_and_only_return_matching_path(api):
    claim(api, "src/a.py")
    claim(api, "src/b.py", "b")
    assert len(api.get("/v1/claims").json()["claims"]) == 2
    rows = api.get("/v1/claims?path=src/b.py").json()["claims"]
    assert len(rows) == 1 and rows[0]["agent"] == "agent-b"
    assert api.get("/v1/claims?path=missing.py").json()["claims"] == []
    assert api.get("/v1/claims?path=../secret").status == 400
