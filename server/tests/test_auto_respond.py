import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from agentmesh_server.config import init_config
from agentmesh_server.http import create_server
from conftest import LocalAPI


def test_auto_respond_defaults_on_and_isolated_per_authenticated_member(api):
    assert api.get("/v1/settings").json() == {"agent": "agent-a", "auto_respond": True}
    assert api.get("/v1/settings", "b").json() == {"agent": "agent-b", "auto_respond": True}
    result = api.post("/v1/settings", {"auto_respond": False, "agent": "agent-b"})
    assert result.status == 200
    assert result.json() == {"agent": "agent-a", "auto_respond": False}
    assert api.get("/v1/settings", "b").json()["auto_respond"] is True
    assert api.post("/v1/settings", {"auto_respond": True}).json()["auto_respond"] is True


@pytest.mark.parametrize("role,status", [(None, 401), ("invalid", 401), ("viewer", 403)])
def test_auto_respond_settings_require_member_authentication(api, role, status):
    assert api.get("/v1/settings", role).status == status
    assert api.post("/v1/settings", {"auto_respond": False}, role).status == status


@pytest.mark.parametrize("body", [{}, {"auto_respond": None}, {"auto_respond": 0},
                                      {"auto_respond": 1}, {"auto_respond": "off"},
                                      {"auto_respond": []}, {"auto_respond": {}}])
def test_auto_respond_settings_require_a_boolean(api, body):
    assert api.post("/v1/settings", body).status == 400
    assert api.get("/v1/settings").json()["auto_respond"] is True


def test_auto_respond_settings_survive_server_restart(tmp_path):
    config_dir = tmp_path / "settings"
    config = init_config(config_dir)
    for restarted in (False, True):
        server = create_server(config_dir, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            api = LocalAPI(server, config)
            assert api.get("/v1/settings").json()["auto_respond"] is (not restarted)
            assert api.post("/v1/settings", {"auto_respond": False}).status == 200
            assert api.get("/v1/settings", "b").json()["auto_respond"] is True
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            assert not thread.is_alive()


def new_huddle(api):
    result = api.post("/v1/huddles", {"kind": "design", "topic": "Review the contract"})
    assert result.status == 201
    return f"/v1/huddles/{result.json()['id']}"


def new_question(api):
    result = api.post("/v1/questions", {"to": "agent-b", "text": "Which contract?"})
    assert result.status == 201
    return f"/v1/questions/{result.json()['id']}/answer"


def test_automatic_huddle_post_is_once_only_even_for_concurrent_clients(api):
    root = new_huddle(api)
    body = {"kind": "idea", "text": "Follow the published contract.", "auto_respond": True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: api.post(root + "/posts", body, "b").status, range(2)))
    assert sorted(statuses) == [201, 409]
    posts = api.get(root).json()["huddle"]["posts"]
    assert len(posts) == 1
    assert posts[0]["author"] == "agent-b"
    assert api.post(root + "/posts", {"kind": "idea", "text": "Manual follow-up."}, "b").status == 201


def test_automatic_huddle_post_does_not_repeat_an_existing_manual_post(api):
    root = new_huddle(api)
    assert api.post(root + "/posts", {"kind": "idea", "text": "Already contributed."}, "b").status == 201
    duplicate = api.post(root + "/posts", {"kind": "idea", "text": "Automatic reply.", "auto_respond": True}, "b")
    assert duplicate.status == 409
    assert "already contributed" in duplicate.json()["error"]


def test_disabled_auto_respond_blocks_automatic_replies_not_manual_replies(api):
    huddle, answer = new_huddle(api), new_question(api)
    assert api.post("/v1/settings", {"auto_respond": False}, "b").status == 200
    for path, body in ((huddle + "/posts", {"kind": "idea", "text": "Reply"}),
                       (answer, {"text": "Use version one."})):
        blocked = api.post(path, {**body, "auto_respond": True}, "b")
        assert blocked.status == 409
        assert "disabled" in blocked.json()["error"]
        assert api.post(path, body, "b").status in (200, 201)


@pytest.mark.parametrize("automatic", [None, 1, "true", []])
def test_reply_auto_respond_marker_requires_a_boolean(api, automatic):
    huddle, answer = new_huddle(api), new_question(api)
    assert api.post(huddle + "/posts", {"kind": "idea", "text": "Reply", "auto_respond": automatic}, "b").status == 400
    assert api.post(answer, {"text": "Reply", "auto_respond": automatic}, "b").status == 400


@pytest.mark.parametrize("text", ["x" * 501, "😀" * 251])
def test_automatic_replies_are_capped_in_utf16_units(api, text):
    huddle, answer = new_huddle(api), new_question(api)
    assert api.post(huddle + "/posts", {"kind": "idea", "text": text, "auto_respond": True}, "b").status == 400
    assert api.post(answer, {"text": text, "auto_respond": True}, "b").status == 400


def test_automatic_replies_are_sanitized_and_question_answer_is_once_only(api):
    root, answer = new_huddle(api), new_question(api)
    text = "\ufeff Use\x00 the\x85 contract. \ufeff"
    assert api.post(root + "/posts", {"kind": "idea", "text": text, "auto_respond": True}, "b").status == 201
    assert api.get(root).json()["huddle"]["posts"][0]["text"] == "Use the contract."
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: api.post(answer, {"text": text, "auto_respond": True}, "b").status, range(2)))
    assert sorted(statuses) == [200, 409]
    assert api.get("/v1/questions", "b").json()["questions"][0]["answer"] == "Use the contract."
