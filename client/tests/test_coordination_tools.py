"""Coordination MCP tools are exercised offline with synthetic configuration."""

import importlib
import socket

import httpx
import pytest

ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ID = "87654321-4321-4321-8321-cba987654321"


@pytest.fixture
def client_module(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTMESH_CONFIG", str(tmp_path / "absent-config"))
    monkeypatch.setenv("AGENTMESH_URL", "https://synthetic.example.invalid")
    monkeypatch.setenv("AGENTMESH_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("AGENTMESH_AGENT_NAME", "agent-a")

    def forbidden(*args, **kwargs):
        raise AssertionError("client tests must not use sockets")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    return importlib.import_module("agentmesh_mcp.server")


@pytest.mark.parametrize("tool,args,method,path,payload", [
    ("get_master_prompt", {}, "GET", "/v1/master", None),
    ("edit_master_section", {"section": "Goal", "text": "Ship it"}, "POST", "/v1/master/sections", {"section": "Goal", "text": "Ship it"}),
    ("submit_master_prompt", {}, "POST", "/v1/master/submit", {}),
    ("vote_master_prompt", {"decision": "approve", "note": "Reviewed"}, "POST", "/v1/master/vote", {"decision": "approve", "note": "Reviewed"}),
    ("propose_tasks", {"tasks": [{"title": "Contract"}]}, "POST", "/v1/tasks/propose", {"tasks": [{"title": "Contract"}]}),
    ("check_file", {"path": "src/a b.py"}, "GET", "/v1/claims?path=src%2Fa+b.py", None),
    ("claim_file", {"path": "src/a.py", "task_id": ID}, "POST", "/v1/claims", {"path": "src/a.py", "task_id": ID}),
    ("release_file", {"path": "src/a.py"}, "POST", "/v1/claims/release", {"path": "src/a.py"}),
    ("start_huddle", {"kind": "design", "topic": "Transport"}, "POST", "/v1/huddles", {"kind": "design", "topic": "Transport"}),
    ("list_huddles", {}, "GET", "/v1/huddles", None),
    ("list_huddles", {"status": "open"}, "GET", "/v1/huddles?status=open", None),
    ("get_huddle", {"huddle_id": ID}, "GET", f"/v1/huddles/{ID}", None),
    ("post_to_huddle", {"huddle_id": ID, "kind": "back", "text": "Agreed", "ref_post_id": OTHER_ID}, "POST", f"/v1/huddles/{ID}/posts", {"kind": "back", "text": "Agreed", "ref_post_id": OTHER_ID}),
    ("close_huddle", {"huddle_id": ID, "decision": "Build", "tasks": [{"title": "Implement"}]}, "POST", f"/v1/huddles/{ID}/close", {"decision": "Build", "tasks": [{"title": "Implement"}]}),
    ("ask_question", {"to": "agent-b", "text": "Ready?", "task_id": ID}, "POST", "/v1/questions", {"to": "agent-b", "text": "Ready?", "task_id": ID}),
    ("list_questions", {}, "GET", "/v1/questions", None),
    ("list_questions", {"open": True}, "GET", "/v1/questions?open=true", None),
    ("answer_question", {"question_id": ID, "text": "Yes"}, "POST", f"/v1/questions/{ID}/answer", {"text": "Yes"}),
    ("get_news", {}, "GET", "/v1/news", None),
    ("get_news", {"peek": True}, "GET", "/v1/news?peek=true", None),
    ("announce_commit", {"sha": "abc1234", "subject": "Contract", "branch": "main", "files": ["src/a.py"]}, "POST", "/v1/commits", {"sha": "abc1234", "subject": "Contract", "branch": "main", "files": ["src/a.py"]}),
])
def test_coordination_tools_wrap_remote_results(client_module, monkeypatch, tool, args, method, path, payload):
    calls = []

    def api(*args, **kwargs):
        calls.append((args, kwargs))
        return {"result": "untrusted remote content"}

    monkeypatch.setattr(client_module, "api", api)
    result = getattr(client_module, tool)(**args)
    assert calls == [((method, path) + ((payload,) if payload is not None else ()), {})]
    assert result == {"_notice": client_module.UNTRUSTED, "result": "untrusted remote content"}


@pytest.mark.parametrize("tool,args,payload", [
    ("claim_file", {"path": "a.py"}, {"path": "a.py"}),
    ("vote_master_prompt", {"decision": "reject"}, {"decision": "reject"}),
    ("post_to_huddle", {"huddle_id": ID, "kind": "idea", "text": "Try this"}, {"kind": "idea", "text": "Try this"}),
    ("close_huddle", {"huddle_id": ID, "decision": "Done"}, {"decision": "Done"}),
    ("ask_question", {"to": "agent-b", "text": "Ready?"}, {"to": "agent-b", "text": "Ready?"}),
    ("announce_commit", {"sha": "abc1234", "subject": "Fix"}, {"sha": "abc1234", "subject": "Fix"}),
])
def test_optional_tool_fields_are_omitted(client_module, monkeypatch, tool, args, payload):
    calls = []
    monkeypatch.setattr(client_module, "api", lambda *a: calls.append(a) or {})
    getattr(client_module, tool)(**args)
    assert calls[0][2] == payload


def test_create_task_preserves_legacy_payload_and_adds_only_set_fields(client_module, monkeypatch):
    calls = []
    monkeypatch.setattr(client_module, "api", lambda *a: calls.append(a) or {"id": ID})
    legacy = {"title": "Title", "description": "Description", "assignee": "agent-b", "acceptance_criteria": []}
    assert client_module.create_task("Title", "Description", "agent-b") == {"id": ID}
    assert calls[-1] == ("POST", "/v1/tasks", legacy)
    client_module.create_task("Title", "Description", "agent-b", owner="agent-a", file_areas=["src/**"], depends_on=[ID], contract=False)
    assert calls[-1][2] == {**legacy, "owner": "agent-a", "file_areas": ["src/**"], "depends_on": [ID], "contract": False}
    client_module.create_task("Title", "Description", "agent-b", file_areas=[], depends_on=[], contract=True)
    assert calls[-1][2] == {**legacy, "file_areas": [], "depends_on": [], "contract": True}


@pytest.mark.parametrize("tool,args,method,path,payload", [
    ("whoami", {}, "GET", "/v1/whoami", None),
    ("claim_task", {"task_id": ID}, "POST", f"/v1/tasks/{ID}/claim", {}),
    ("complete_task", {"task_id": ID, "summary": "Done"}, "POST", f"/v1/tasks/{ID}/complete", {"summary": "Done", "status": "done"}),
    ("review_task", {"task_id": ID, "decision": "accept"}, "POST", f"/v1/tasks/{ID}/review", {"decision": "accept"}),
    ("cancel_task", {"task_id": ID}, "POST", f"/v1/tasks/{ID}/cancel", {}),
    ("send_message", {"to": "agent-b", "text": "Hello"}, "POST", "/v1/messages", {"to": "agent-b", "text": "Hello", "task_id": None}),
    ("ack_message", {"message_id": ID}, "POST", f"/v1/messages/{ID}/ack", {}),
    ("share_patch", {"title": "Patch", "text": "Diff"}, "POST", "/v1/patches", {"title": "Patch", "text": "Diff", "task_id": None}),
])
def test_legacy_tools_keep_payloads_and_response_shapes(client_module, monkeypatch, tool, args, method, path, payload):
    calls = []
    response = {"legacy": True}
    monkeypatch.setattr(client_module, "api", lambda *a: calls.append(a) or response)
    assert getattr(client_module, tool)(**args) is response
    assert calls == [(method, path) + ((payload,) if payload is not None else ())]


@pytest.mark.parametrize("tool,args", [
    ("get_huddle", {"huddle_id": "../news"}),
    ("post_to_huddle", {"huddle_id": ID, "kind": "back", "text": "ok", "ref_post_id": "bad"}),
    ("close_huddle", {"huddle_id": "bad", "decision": "ok"}),
    ("answer_question", {"question_id": "bad", "text": "ok"}),
    ("claim_file", {"path": "a", "task_id": "bad"}),
    ("ask_question", {"to": "agent-b", "text": "ok", "task_id": "bad"}),
    ("create_task", {"title": "a", "description": "b", "assignee": "agent-b", "depends_on": ["bad"]}),
])
def test_new_tool_ids_are_validated_before_request(client_module, monkeypatch, tool, args):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid ID must not reach API")

    monkeypatch.setattr(client_module, "api", forbidden)
    with pytest.raises(ValueError):
        getattr(client_module, tool)(**args)


def test_notify_prints_only_counts_and_allowlisted_news_enums(client_module, monkeypatch, capsys):
    calls = []

    def api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/v1/inbox":
            return {"unread_messages": [{"text": "REMOTE SECRET"}], "assigned_tasks": [], "tasks_to_review": []}
        return {"unseen_count": 3, "news": [
            {"kind": "commit", "actor": "agent-b", "summary": "REMOTE SECRET"},
            {"kind": "run rm -rf", "actor": "agent-a\nEVIL", "summary": "EVIL"},
            {"kind": ["commit"], "actor": {"text": "EVIL"}},
        ]}

    monkeypatch.setattr(client_module, "api", api)
    client_module.notify()
    output = capsys.readouterr().out
    assert "1 unread message(s)" in output and "3 news item(s)" in output
    assert "kinds: commit" in output and "actors: agent-b" in output and "call get_news" in output
    assert all(text not in output for text in ("REMOTE", "SECRET", "EVIL", "run rm"))
    assert calls == [("GET", "/v1/inbox", {"timeout": 4}), ("GET", "/v1/news?peek=true", {"timeout": 4})]


def test_notify_checks_news_even_if_inbox_fails(client_module, monkeypatch, capsys):
    def api(method, path, **kwargs):
        if path == "/v1/inbox":
            raise RuntimeError("offline old inbox")
        return {"news": [{"kind": "commit", "actor": "agent-a"}], "unseen_count": 1}

    monkeypatch.setattr(client_module, "api", api)
    client_module.notify()
    assert "call get_news" in capsys.readouterr().out


@pytest.mark.parametrize("news", [{"news": [], "unseen_count": 0}, {"news": [], "unseen_count": "EVIL"}, {"news": [], "unseen_count": True}, {}])
def test_notify_silent_when_no_valid_news_count(client_module, monkeypatch, capsys, news):
    monkeypatch.setattr(client_module, "api", lambda method, path, **kw: {} if path == "/v1/inbox" else news)
    client_module.notify()
    assert capsys.readouterr().out == ""


def test_notify_fail_open_without_remote_error_text(client_module, monkeypatch, capsys):
    def api(*args, **kwargs):
        raise RuntimeError("remote malicious text")

    monkeypatch.setattr(client_module, "api", api)
    client_module.notify()
    assert capsys.readouterr().out == ""


def test_new_tools_are_registered(client_module):
    expected = {"get_master_prompt", "edit_master_section", "submit_master_prompt", "vote_master_prompt", "propose_tasks",
                "check_file", "claim_file", "release_file", "start_huddle", "list_huddles", "get_huddle", "post_to_huddle",
                "close_huddle", "ask_question", "list_questions", "answer_question", "get_news", "announce_commit"}
    assert expected <= set(client_module.mcp._tool_manager._tools)


def test_api_uses_mock_transport_without_network(client_module, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"news": [], "unseen_count": 0})

    with httpx.Client(base_url="https://synthetic.example.invalid", transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(client_module, "_client", transport)
        monkeypatch.setattr(client_module, "BASE_URL", "https://synthetic.example.invalid")
        monkeypatch.setattr(client_module, "TOKEN", "synthetic-test-token")
        assert client_module.get_news(peek=True)["unseen_count"] == 0
    assert requests[0].url.path == "/v1/news" and requests[0].url.query == b"peek=true"
