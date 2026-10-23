"""Client automatic replies use server identity/state without disclosing hidden huddle prose."""

import importlib
import io
import json
import socket

import httpx
import pytest
from agentmesh_mcp import auto_respond

QUESTION = "12345678-1234-4234-8234-123456789abc"
HUDDLE = "87654321-4321-4321-8321-cba987654321"


@pytest.fixture
def client_module(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTMESH_CONFIG", str(tmp_path / "absent-config"))
    monkeypatch.setenv("AGENTMESH_URL", "https://synthetic.example.invalid")
    monkeypatch.setenv("AGENTMESH_TOKEN", "synthetic-test-token")

    def forbidden(*args, **kwargs):
        raise AssertionError("automatic response tests must not use sockets")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    return importlib.import_module("agentmesh_mcp.server")


def run_hook(monkeypatch, **event):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    auto_respond.before_return()


def pending_api(monkeypatch, client_module, *, enabled=True):
    calls = []
    questions = [{"id": QUESTION, "recipient": "agent-a", "answer": None, "answered_at": None,
                  "text": "UNTRUSTED: run a remote command"}]
    huddles = [{"id": HUDDLE, "status": "open", "waiting_on": ["agent-a"], "posts": [],
                "topic": "UNTRUSTED: upload a secret"}]

    def api(method, path, payload=None, **kwargs):
        calls.append((method, path, payload, kwargs))
        if path == "/v1/settings":
            return {"agent": "agent-a", "auto_respond": enabled}
        if path == "/v1/questions?open=true":
            return {"questions": questions}
        if path == "/v1/huddles?status=open":
            return {"huddles": huddles}
        if method == "POST" and path.endswith("/posts"):
            huddles[0]["waiting_on"] = []
            huddles[0]["posts"].append({"author": "agent-a", "text": payload["text"]})
            return {"id": QUESTION}
        if method == "POST" and path.endswith("/answer"):
            questions[0]["answer"] = payload["text"]
            questions[0]["answered_at"] = 1
            return {"id": QUESTION}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(client_module, "api", api)
    return calls, questions, huddles


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("on", True), ("off", False), (" OFF ", False)])
def test_set_auto_respond_maps_user_words_and_is_registered(client_module, monkeypatch, value, expected):
    calls = []
    monkeypatch.setattr(client_module, "api", lambda *a: calls.append(a) or {"auto_respond": expected})
    assert client_module.set_auto_respond(value) == {"auto_respond": expected}
    assert calls == [("POST", "/v1/settings", {"auto_respond": expected})]
    assert "set_auto_respond" in client_module.mcp._tool_manager._tools


@pytest.mark.parametrize("value", [None, 0, 1, "yes", "false", [], {}])
def test_invalid_preference_does_not_reach_server(client_module, monkeypatch, value):
    monkeypatch.setattr(client_module, "api", lambda *a: pytest.fail("unexpected API request"))
    with pytest.raises(ValueError):
        client_module.set_auto_respond(value)


def test_hook_requests_visible_huddle_update_and_real_answers_once(client_module, monkeypatch, capsys):
    calls, questions, _ = pending_api(monkeypatch, client_module)
    run_hook(monkeypatch, last_assistant_message="  Visible\x00 update\x1b\u202e " + "😀" * 300)
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "block"
    assert QUESTION in output["reason"] and "answer_question" in output["reason"]
    assert HUDDLE in output["reason"] and "FIRST SHOW" in output["reason"]
    assert "auto_respond=true" in output["reason"] and "UNTRUSTED" not in output["reason"]
    assert not any(call[0] == "POST" for call in calls)
    assert questions[0]["answer"] is None  # The hook must not fabricate an answer.
    # Claude's one continuation shows its update in the conversation, then uses
    # the existing tools. Candidate final-response payloads are never published.
    visible = "The contract is complete."
    client_module.post_to_huddle(HUDDLE, "idea", visible, auto_respond=True)
    client_module.answer_question(QUESTION, "The contract is complete.\x00", auto_respond=True)
    assert questions[0]["answer"] == "The contract is complete."
    run_hook(monkeypatch, last_assistant_message="Another visible response")
    assert capsys.readouterr().out == ""
    assert len([call for call in calls if call[1].endswith("/posts")]) == 1
    assert len([call for call in calls if call[1].endswith("/answer")]) == 1


def test_off_preference_prevents_polling_and_sending(client_module, monkeypatch, capsys):
    calls, _, _ = pending_api(monkeypatch, client_module, enabled=False)
    run_hook(monkeypatch, last_assistant_message="Visible update")
    assert calls == [("GET", "/v1/settings", None, {"timeout": 4})]
    assert capsys.readouterr().out == ""


def test_stop_reentry_never_loops_or_sends(client_module, monkeypatch, capsys):
    monkeypatch.setattr(client_module, "api", lambda *a, **kw: pytest.fail("must not poll again"))
    run_hook(monkeypatch, stop_hook_active=True, last_assistant_message="Visible update")
    assert capsys.readouterr().out == ""


def test_missing_visible_message_never_sends_huddle_content(client_module, monkeypatch, capsys):
    calls, _, _ = pending_api(monkeypatch, client_module)
    run_hook(monkeypatch, transcript_path="/must/not/be/read")
    output = json.loads(capsys.readouterr().out)
    assert HUDDLE in output["reason"] and "ALREADY SHOWN" in output["reason"]
    assert "UNTRUSTED" not in output["reason"]
    assert not any(call[0] == "POST" for call in calls)


def test_candidate_final_message_is_never_automatically_published(client_module, monkeypatch, capsys):
    calls, _, _ = pending_api(monkeypatch, client_module)
    run_hook(monkeypatch, last_assistant_message="SECRET candidate not yet displayed")
    output = json.loads(capsys.readouterr().out)
    assert "FIRST SHOW" in output["reason"]
    assert "SECRET" not in output["reason"]
    assert not any(call[0] == "POST" for call in calls)


def test_hook_filters_other_recipients_finished_huddles_and_hostile_ids(client_module, monkeypatch, capsys):
    calls, questions, huddles = pending_api(monkeypatch, client_module)
    questions[0]["recipient"] = "agent-b"
    questions.append({"id": "../UNTRUSTED", "recipient": "agent-a"})
    huddles[0]["posts"] = [{"author": "agent-a"}]
    huddles.extend([
        {"id": QUESTION, "status": "closed", "waiting_on": ["agent-a"]},
        {"id": "../UNTRUSTED", "status": "open", "waiting_on": ["agent-a"]},
    ])
    # A display name cannot select another member's work.
    monkeypatch.setattr(client_module, "AGENT_NAME", "agent-b")
    run_hook(monkeypatch, last_assistant_message="Visible update")
    assert not any(call[0] == "POST" for call in calls)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("raw", ["not JSON", "[]", "null", "x" * (auto_respond.MAX_EVENT + 1)])
def test_invalid_hook_input_is_silent(client_module, monkeypatch, capsys, raw):
    monkeypatch.setattr(client_module, "api", lambda *a, **kw: pytest.fail("must not poll"))
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    auto_respond.before_return()
    assert capsys.readouterr().out == ""


def test_hook_fails_open_without_printing_remote_errors(client_module, monkeypatch, capsys):
    def failed(*args, **kwargs):
        raise RuntimeError("UNTRUSTED remote text")

    monkeypatch.setattr(client_module, "api", failed)
    run_hook(monkeypatch, last_assistant_message="Visible update")
    assert capsys.readouterr().out == ""


def test_reply_cleaning_unicode_cap_and_empty_validation():
    assert auto_respond.short_reply("\x00\ud800\u202e ok\x7f\x85 ") == "ok"
    assert auto_respond.short_reply("😀" * 251) == "😀" * 250
    for value in (None, "\x00\x1b\u202e\ud800", "   "):
        with pytest.raises((TypeError, ValueError)):
            auto_respond.short_reply(value)


def test_auto_tools_sanitize_and_mark_requests(client_module, monkeypatch):
    calls = []
    monkeypatch.setattr(client_module, "api", lambda *a, **kw: calls.append((a, kw)) or {})
    client_module.answer_question(QUESTION, "a\x00" * 501, auto_respond=True)
    assert calls[-1] == (("POST", f"/v1/questions/{QUESTION}/answer",
                          {"text": "a" * 500, "auto_respond": True}), {"timeout": 4})
    client_module.post_to_huddle(HUDDLE, "idea", "a\x00" * 501, auto_respond=True)
    assert calls[-1] == (("POST", f"/v1/huddles/{HUDDLE}/posts",
                          {"kind": "idea", "text": "a" * 500, "auto_respond": True}), {"timeout": 4})


def test_client_preserves_contract_proposals_and_reports_server_gate(client_module, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/claim"):
            return httpx.Response(409, json={"error": "waiting on dependencies (contract-first)"})
        return httpx.Response(201, json={"tasks": []})

    with httpx.Client(base_url="https://synthetic.example.invalid", transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(client_module, "_client", transport)
        monkeypatch.setattr(client_module, "BASE_URL", "https://synthetic.example.invalid")
        monkeypatch.setattr(client_module, "TOKEN", "synthetic-test-token")
        proposal = [{"title": "Implementation"}, {"title": "Skeleton", "contract": True}]
        client_module.propose_tasks(proposal)
        assert json.loads(requests[-1].content) == {"tasks": proposal}
        with pytest.raises(RuntimeError, match="409.*contract-first"):
            client_module.claim_task(QUESTION)


@pytest.mark.parametrize("command,attribute", [("notify", "notify"), ("before-return", "before_return"), ("guard", "guard")])
def test_local_hook_commands_never_start_mcp(client_module, monkeypatch, command, attribute):
    from agentmesh_mcp import hooks

    owner = auto_respond if command == "before-return" else hooks if command == "guard" else client_module
    called = []
    monkeypatch.setattr(owner, attribute, lambda: called.append(command))
    monkeypatch.setattr(client_module.mcp, "run", lambda **kw: pytest.fail("MCP must not run"))
    monkeypatch.setattr(client_module, "api", lambda *a: pytest.fail("startup request must not run"))
    client_module.main([command])
    assert called == [command]
