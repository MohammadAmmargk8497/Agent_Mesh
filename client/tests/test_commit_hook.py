"""Post-commit helpers never run real git commands or make network requests in tests."""

import shlex
import socket
import subprocess
from types import SimpleNamespace

import pytest
from agentmesh_mcp import hooks


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("commit hook tests must not run programs or use sockets")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(hooks.subprocess, "run", forbidden)
    monkeypatch.setattr(hooks, "_api", forbidden)


def test_commit_announces_metadata_with_bounded_shell_free_subprocesses(monkeypatch):
    calls, posts = [], []
    responses = ["a" * 40 + "\n", "Implement contract\n", "main\n", "src/a b.py\0README.md\0"]

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=responses.pop(0))

    monkeypatch.setattr(hooks.subprocess, "run", run)
    monkeypatch.setattr(hooks, "_api", lambda *args: posts.append(args) or {})
    assert hooks.commit_hook([]) is None
    assert posts == [("POST", "/v1/commits", {"sha": "a" * 40, "subject": "Implement contract", "branch": "main", "files": ["src/a b.py", "README.md"]})]
    assert [call[0] for call in calls] == [
        ["git", "rev-parse", "HEAD"], ["git", "log", "-1", "--format=%s"],
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", "HEAD"],
    ]
    assert all(kwargs == {"capture_output": True, "text": True, "check": True, "shell": False, "timeout": 5} for _, kwargs in calls)


def test_commit_caps_files_and_preserves_spaces(monkeypatch):
    responses = iter(["a" * 40, "A commit", "HEAD", " leading space.py\0" + "\0".join(f"src/{i}" for i in range(60)) + "\0"])
    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=next(responses)))
    payloads = []
    monkeypatch.setattr(hooks, "_api", lambda method, path, data: payloads.append(data) or {})
    hooks.commit_hook([])
    assert len(payloads[0]["files"]) == 50 and payloads[0]["files"][0] == " leading space.py"


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("git", 5), subprocess.CalledProcessError(1, "git"), OSError("no git")])
def test_commit_never_fails_on_subprocess_error(monkeypatch, capsys, error):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(hooks.subprocess, "run", run)
    assert hooks.commit_hook([]) is None
    assert capsys.readouterr().out == ""


def test_commit_never_fails_on_api_error(monkeypatch, capsys):
    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="synthetic"))

    def unavailable(*args):
        raise RuntimeError("untrusted remote error")

    monkeypatch.setattr(hooks, "_api", unavailable)
    assert hooks.commit_hook([]) is None
    assert capsys.readouterr().out == ""


def test_install_creates_executable_marked_hook_and_can_refresh_it(monkeypatch, tmp_path):
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    interpreter = str(tmp_path / "my Python" / "python3")
    monkeypatch.setattr(hooks.sys, "executable", interpreter)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=str(hook) + "\n")

    monkeypatch.setattr(hooks.subprocess, "run", run)
    assert hooks.commit_hook(["--install"]) is None
    assert hook.read_text() == "#!/bin/sh\n" + hooks.MARKER + "\n" + shlex.quote(interpreter) + " -m agentmesh_mcp.hooks >/dev/null 2>&1 || :\nexit 0\n"
    assert hook.stat().st_mode & 0o111 == 0o111
    before = hook.read_bytes()
    hooks.commit_hook(["--install"])
    assert hook.read_bytes() == before
    assert calls[0][0] == ["git", "rev-parse", "--git-path", "hooks/post-commit"]


def test_install_refuses_foreign_hook(monkeypatch, tmp_path, capsys):
    hook = tmp_path / "post-commit"
    original = "#!/bin/sh\necho existing hook\n"
    hook.write_text(original)
    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=str(hook)))
    assert hooks.commit_hook(["--install"]) is None
    assert hook.read_text() == original
    assert "refusing to overwrite" in capsys.readouterr().err


@pytest.mark.parametrize("dangling", [False, True])
def test_install_refuses_symlink_even_when_marked(monkeypatch, tmp_path, capsys, dangling):
    target = tmp_path / "target"
    if not dangling:
        target.write_text(hooks.MARKER + "\nkeep this content\n")
    hook = tmp_path / "post-commit"
    hook.symlink_to(target)
    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=str(hook)))
    assert hooks.commit_hook(["--install"]) is None
    assert hook.is_symlink()
    if not dangling:
        assert target.read_text() == hooks.MARKER + "\nkeep this content\n"
    assert "refusing to overwrite" in capsys.readouterr().err


def test_unknown_arguments_are_noop():
    assert hooks.commit_hook(["--unknown"]) is None
