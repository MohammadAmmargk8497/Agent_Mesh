"""Editor-claim guard tests use synthetic repositories and an in-process API fake."""

import io
import json
import socket

import pytest
from agentmesh_mcp import hooks


@pytest.fixture
def guard_context(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTMESH_CLAIM_MODE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("guard must not open a socket or invoke git")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(hooks.subprocess, "run", forbidden)
    return tmp_path


def hook_input(monkeypatch, cwd, tool="Edit", **data):
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO(json.dumps({"cwd": str(cwd), "tool_name": tool, "tool_input": data})))


@pytest.mark.parametrize("mode,expected", [(None, "ask"), ("deny", "deny"), ("other", "ask")])
def test_guard_asks_or_denies_for_another_members_claim(guard_context, monkeypatch, capsys, mode, expected):
    calls = []
    if mode is not None:
        monkeypatch.setenv("AGENTMESH_CLAIM_MODE", mode)
    hook_input(monkeypatch, guard_context, file_path=str(guard_context / "src" / "a b.py"))

    def api(*args):
        calls.append(args)
        return {"conflict": {"path": "src/a b.py", "member_id": "mb", "member_name": "agent-b", "summary": "EVIL"}}

    monkeypatch.setattr(hooks, "_api", api)
    assert hooks.guard() is None
    result = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert result["hookEventName"] == "PreToolUse" and result["permissionDecision"] == expected
    assert "agent-b" in result["permissionDecisionReason"] and "EVIL" not in result["permissionDecisionReason"]
    assert calls == [("POST", "/api/file-edit", {"path": "src/a b.py"})]


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_guard_maps_edit_tools_and_notebooks(guard_context, monkeypatch, capsys, tool, key):
    hook_input(monkeypatch, guard_context, tool=tool, **{key: "notebook.ipynb"})
    monkeypatch.setattr(hooks, "_api", lambda *args: {"conflict": {"member_id": "ma", "member_name": "agent-a"}})
    hooks.guard()
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_finds_repo_root_from_a_subdirectory(guard_context, monkeypatch, capsys):
    subdirectory = guard_context / "src"
    subdirectory.mkdir()
    hook_input(monkeypatch, subdirectory, file_path="a.py")
    calls = []
    monkeypatch.setattr(hooks, "_api", lambda *args: calls.append(args) or {"conflict": None})
    hooks.guard()
    assert calls == [("POST", "/api/file-edit", {"path": "src/a.py"})]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("response", [
    {"conflict": None},
    {"conflict": {}},
    {"conflict": {"member_name": "agent-b\nEVIL"}},
    {"conflict": {"member_name": "<script>"}},
    {"conflict": {"member_name": None}},
    {"conflict": []},
    {},
])
def test_guard_is_silent_for_free_own_or_invalid_claims(guard_context, monkeypatch, capsys, response):
    hook_input(monkeypatch, guard_context, file_path="a.py")
    monkeypatch.setattr(hooks, "_api", lambda *args: response)
    hooks.guard()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("event", [
    {"tool_name": "Read", "tool_input": {"file_path": "a.py"}},
    {"tool_name": "Bash", "tool_input": {"command": "echo harmless"}},
    {"tool_name": "Edit", "tool_input": {}},
    {"tool_name": "Edit", "tool_input": {"file_path": "bad\x00path"}},
    {"tool_name": "Edit", "tool_input": {"file_path": "../outside.py"}},
    [],
])
def test_guard_ignores_nonediting_or_outside_paths(guard_context, monkeypatch, capsys, event):
    if isinstance(event, dict):
        event["cwd"] = str(guard_context)
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO(json.dumps(event)))

    def forbidden(*args):
        raise AssertionError("this event should not query claims")

    monkeypatch.setattr(hooks, "_api", forbidden)
    hooks.guard()
    assert capsys.readouterr().out == ""


def test_guard_does_not_follow_symlink_outside_repo(guard_context, monkeypatch, tmp_path, capsys):
    (guard_context / "outside-link").symlink_to(guard_context.parent, target_is_directory=True)
    hook_input(monkeypatch, guard_context, file_path="outside-link/a.py")
    calls = []
    monkeypatch.setattr(hooks, "_api", lambda *args: calls.append(args) or {})
    hooks.guard()
    assert calls == [] and capsys.readouterr().out == ""


def test_guard_fail_open_on_api_error(guard_context, monkeypatch, capsys):
    hook_input(monkeypatch, guard_context, file_path="a.py")

    def unavailable(*args):
        raise RuntimeError("remote malicious text")

    monkeypatch.setattr(hooks, "_api", unavailable)
    hooks.guard()
    assert capsys.readouterr().out == ""


def test_guard_fail_open_on_malformed_input(guard_context, monkeypatch, capsys):
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO("not JSON"))
    hooks.guard()
    assert capsys.readouterr().out == ""
