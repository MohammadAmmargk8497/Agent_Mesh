"""Explicit hook installation only edits synthetic Claude settings, never runs hooks."""

import json
import shlex
import socket
import stat

import pytest
from agentmesh_mcp import hooks


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTMESH_CONFIG", str(tmp_path / "absent-config"))

    def forbidden(*args, **kwargs):
        raise AssertionError("hook installation must not run commands or use the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(hooks.subprocess, "run", forbidden)
    monkeypatch.setattr(hooks, "_api", forbidden)


def invoke(capsys, path=None, apply=False):
    args = ["--settings", str(path)] if path is not None else []
    hooks.install_hooks(args + (["--apply"] if apply else []))
    return json.loads(capsys.readouterr().out)


def test_default_dry_run_prints_json_diff_without_creating_home(tmp_path, capsys):
    result = invoke(capsys)
    settings = tmp_path / "home" / ".claude" / "settings.json"
    assert result["settings"] == str(settings)
    assert result["dry_run"] and result["changed"] and not result["applied"]
    assert result["backup"] is None
    assert not (tmp_path / "home").exists()
    assert result["diff"][0]["op"] == "add" and result["diff"][0]["path"] == "/hooks"
    assert set(result["diff"][0]["value"]) == {"UserPromptSubmit", "PreToolUse", "Stop"}


def test_custom_path_dry_run_preserves_original_bytes_and_directory(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    original = b'{"theme":"dark", "permissions":{"deny":["Read(private)"]}}\n'
    settings.write_bytes(original)
    before = set(tmp_path.iterdir())
    invoke(capsys, settings)
    assert settings.read_bytes() == original
    assert set(tmp_path.iterdir()) == before


def test_apply_creates_missing_settings_with_restrictive_permissions(tmp_path, capsys):
    settings = tmp_path / "new" / "nested" / "settings.json"
    result = invoke(capsys, settings, apply=True)
    assert result["applied"] and not result["dry_run"] and result["backup"] is None
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.parent.parent.stat().st_mode) == 0o700
    assert set(json.loads(settings.read_text())["hooks"]) == {"UserPromptSubmit", "PreToolUse", "Stop"}


def test_apply_preserves_all_settings_hooks_and_writes_exact_backup_first(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    existing = {"theme": "dark", "permissions": {"allow": ["Read"], "deny": ["Bash(rm *)"]},
                "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "existing news"}]}],
                          "PreToolUse": [{"matcher": "Bash", "custom": True,
                                          "hooks": [{"type": "command", "command": "existing guard"}]}],
                          "Stop": [{"hooks": [{"type": "prompt", "prompt": "existing stop prompt"}]}],
                          "SessionStart": [{"matcher": "startup", "hooks": []}]}}
    original = json.dumps(existing, separators=(",", ":")).encode() + b"\n\n"
    settings.write_bytes(original)
    settings.chmod(0o640)
    replace = hooks.os.replace
    replacements = []

    def checked_replace(source, destination):
        backups = list(tmp_path.glob("settings.json.agentmesh-backup-*"))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert settings.read_bytes() == original
        replacements.append((source, destination))
        return replace(source, destination)

    monkeypatch.setattr(hooks.os, "replace", checked_replace)
    result = invoke(capsys, settings, apply=True)
    merged = json.loads(settings.read_text())
    assert result["applied"] and result["backup"]
    assert len(replacements) == 1
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640
    assert merged["theme"] == existing["theme"] and merged["permissions"] == existing["permissions"]
    for event, groups in existing["hooks"].items():
        assert merged["hooks"][event][:len(groups)] == groups
    assert all(item["op"] == "add" for item in result["diff"])
    assert all(item["path"].endswith("/-") for item in result["diff"])
    assert set(tmp_path.iterdir()) == {settings, tmp_path / result["backup"]}


def test_apply_is_idempotent_and_does_not_create_another_backup(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    first = invoke(capsys, settings, apply=True)
    original = settings.read_bytes()
    modification = settings.stat().st_mtime_ns
    second = invoke(capsys, settings, apply=True)
    assert first["applied"] and first["backup"]
    assert second["diff"] == [] and not second["changed"] and not second["applied"]
    assert second["backup"] is None
    assert settings.read_bytes() == original and settings.stat().st_mtime_ns == modification
    assert len(list(tmp_path.glob("settings.json.agentmesh-backup-*"))) == 1


def test_backups_have_unique_names_across_subsequent_changes(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    first = invoke(capsys, settings, apply=True)
    changed = json.loads(settings.read_text())
    del changed["hooks"]["Stop"]
    settings.write_text(json.dumps(changed))
    before_second = settings.read_bytes()
    second = invoke(capsys, settings, apply=True)
    assert first["backup"] != second["backup"]
    assert (tmp_path / first["backup"]).read_bytes() == b"{}"
    assert (tmp_path / second["backup"]).read_bytes() == before_second


def test_hook_commands_quote_python_and_include_guards_and_stop_timeout(tmp_path, monkeypatch, capsys):
    interpreter = str(tmp_path / "Python's custom installation" / "python3")
    monkeypatch.setattr(hooks.sys, "executable", interpreter)
    result = invoke(capsys, tmp_path / "settings.json")
    groups = result["diff"][0]["value"]
    for event, action in (("UserPromptSubmit", "notify"), ("PreToolUse", "guard"), ("Stop", "before-return")):
        hook = groups[event][0]["hooks"][0]
        assert shlex.split(hook["command"]) == [interpreter, "-m", "agentmesh_mcp.server", action]
        assert hook["type"] == "command" and hook["timeout"] > 0
    assert groups["PreToolUse"][0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    assert groups["Stop"][0]["hooks"][0]["timeout"] == 30


def test_windows_hooks_explicitly_use_powershell_and_quote_literal_interpreter(tmp_path, monkeypatch, capsys):
    interpreter = "C:\\Program Files\\Python's installation\\python.exe"
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks.sys, "executable", interpreter)
    result = invoke(capsys, tmp_path / "settings.json")
    groups = result["diff"][0]["value"]
    for event, action in (("UserPromptSubmit", "notify"), ("PreToolUse", "guard"), ("Stop", "before-return")):
        hook = groups[event][0]["hooks"][0]
        assert hook["shell"] == "powershell"
        assert hook["command"] == "& 'C:\\Program Files\\Python''s installation\\python.exe' -m agentmesh_mcp.server " + action


@pytest.mark.parametrize("raw", [
    b"not json", b"", b"[]", b"null", b'"text"', b'{"hooks":null}', b'{"hooks":[]}',
    b'{"hooks":{"Stop":{}}}', b'{"hooks":{"Stop":[{}]}}', b'{"hooks":{"Stop":[{"hooks":{}}]}}',
    b'{"hooks":{"Stop":[{"matcher":false,"hooks":[]}]}}',
    b'{"hooks":{"Stop":[{"hooks":[false]}]}}',
    b'{"hooks":{"Stop":[{"hooks":[{"type":"command","command":null}]}]}}',
    b'{"hooks":{},"hooks":{}}', b'{"value":NaN}', b'{"value":"\xff"}',
])
def test_malformed_settings_are_refused_before_backup_or_write(tmp_path, capsys, raw):
    settings = tmp_path / "settings.json"
    settings.write_bytes(raw)
    before = set(tmp_path.iterdir())
    with pytest.raises(SystemExit) as exc:
        invoke(capsys, settings, apply=True)
    assert exc.value.code == 2
    assert settings.read_bytes() == raw and set(tmp_path.iterdir()) == before
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("dangling", [False, True])
def test_refuses_settings_symlinks_without_touching_target(tmp_path, capsys, dangling):
    target = tmp_path / "target.json"
    if not dangling:
        target.write_text("{}")
    settings = tmp_path / "settings.json"
    settings.symlink_to(target)
    with pytest.raises(SystemExit) as exc:
        invoke(capsys, settings, apply=True)
    assert exc.value.code == 2 and settings.is_symlink()
    if not dangling:
        assert target.read_text() == "{}"
    assert not list(tmp_path.glob("*agentmesh*"))


def test_refuses_symlink_parent_directory(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    directory = tmp_path / "linked"
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(SystemExit):
        invoke(capsys, directory / "settings.json", apply=True)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("args", [["--unknown"], ["--ap"], ["--settings"], ["extra"], ["--settings", "bad\npath"]])
def test_invalid_arguments_do_not_create_any_files(tmp_path, capsys, args):
    before = set(tmp_path.iterdir())
    with pytest.raises(SystemExit) as exc:
        hooks.install_hooks(args)
    assert exc.value.code == 2 and set(tmp_path.iterdir()) == before


def test_empty_existing_hooks_object_produces_additive_event_diff(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks":{},"other":"keep"}')
    result = invoke(capsys, settings)
    assert [item["path"] for item in result["diff"]] == ["/hooks/UserPromptSubmit", "/hooks/PreToolUse", "/hooks/Stop"]
    assert all(isinstance(item["value"], list) for item in result["diff"])


def test_client_command_dispatches_installation_without_starting_mcp(tmp_path, monkeypatch, capsys):
    from agentmesh_mcp import server

    def forbidden(*args, **kwargs):
        raise AssertionError("explicit installation must not start MCP or query the server")

    monkeypatch.setattr(server, "api", forbidden)
    monkeypatch.setattr(server.mcp, "run", forbidden)
    settings = tmp_path / "settings.json"
    server.main(["install-hooks", "--settings", str(settings)])
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert not settings.exists()


def test_normal_client_startup_never_installs_hooks(monkeypatch):
    from agentmesh_mcp import server

    def forbidden(*args, **kwargs):
        raise AssertionError("normal MCP startup must not install hooks")

    calls = []
    monkeypatch.setattr(hooks, "install_hooks", forbidden)
    monkeypatch.setattr(server, "api", lambda *args: {})
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))
    server.main([])
    assert calls == [{"transport": "stdio"}]


def test_equivalent_commands_with_custom_timeout_are_not_duplicated(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    invoke(capsys, settings, apply=True)
    current = json.loads(settings.read_text())
    current["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 20
    settings.write_text(json.dumps(current))
    result = invoke(capsys, settings, apply=True)
    assert result["diff"] == []
    assert json.loads(settings.read_text())["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 20


@pytest.mark.parametrize("commands", [
    ('"/custom folder/agentmesh-notify"', '"/custom folder/agentmesh-guard"',
     '"/custom folder/agentmesh-mcp" before-return'),
    ('"/old python/python3.11" -m agentmesh_mcp.server notify',
     '"/old python/python3.11" -m agentmesh_mcp.server guard',
     '"/old python/python3.11" -m agentmesh_mcp.server before-return'),
    ("& 'C:\\Python\\python.exe' -m agentmesh_mcp.server notify",
     "& 'C:\\Python\\python.exe' -m agentmesh_mcp.server guard",
     "& 'C:\\Python\\python.exe' -m agentmesh_mcp.server before-return"),
])
def test_documented_manual_and_other_interpreter_hooks_are_not_duplicated(tmp_path, capsys, commands):
    settings = tmp_path / "settings.json"
    existing = {"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": commands[0]}]}],
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                        "hooks": [{"type": "command", "command": commands[1]}]}],
        "Stop": [{"hooks": [{"type": "command", "command": commands[2]}]}],
    }}
    original = json.dumps(existing).encode()
    settings.write_bytes(original)
    result = invoke(capsys, settings, apply=True)
    assert result["diff"] == [] and not result["applied"] and result["backup"] is None
    assert settings.read_bytes() == original


def test_unrelated_command_or_narrow_guard_does_not_suppress_installation(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo agentmesh-notify"}]}],
        "PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "agentmesh-guard"}]}],
    }}))
    result = invoke(capsys, settings, apply=True)
    merged = json.loads(settings.read_text())
    assert result["applied"]
    assert len(merged["hooks"]["UserPromptSubmit"]) == len(merged["hooks"]["PreToolUse"]) == 2
