"""Offline setup checks using only disposable, synthetic credentials."""

import getpass
import os
import shlex
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from agentmesh_mcp import setup_wizard
from agentmesh_mcp.url_policy import is_allowed_url

TOKEN = "synthetic-test-token-not-a-real-credential"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Setup must not access the network or execute another program")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


def supply_answers(monkeypatch, answers, tokens=(TOKEN,)):
    answers = iter(answers)
    tokens = iter(tokens)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(tokens))


def run_setup(directory):
    setup_wizard.main(["--config-dir", str(directory)])


def test_default_location_permissions_and_secret_free_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    executable = tmp_path / "install with spaces" / "agentmesh-mcp"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(sys, "argv", [str(executable.with_name("agentmesh-setup"))])
    supply_answers(monkeypatch, ["agent-a", "https://host.tailnet.ts.net:8443/"])

    setup_wizard.main([])

    path = tmp_path / ".config" / "agentmesh" / "config.env"
    assert path.read_text() == (
        "AGENTMESH_URL=https://host.tailnet.ts.net:8443\n"
        f"AGENTMESH_TOKEN={TOKEN}\nAGENTMESH_AGENT_NAME=agent-a\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    command = next(line for line in output.out.splitlines() if line.startswith("claude mcp add"))
    assert shlex.split(command) == [
        "claude", "mcp", "add", "--scope", "user", "--transport", "stdio", "agentmesh",
        "--env", f"AGENTMESH_CONFIG={path}", "--", str(executable),
    ]
    assert "No connection was tested" in output.out


def test_custom_directory_and_restrictive_mode_with_permissive_umask(monkeypatch, tmp_path):
    directory = tmp_path / "custom config"
    directory.mkdir(mode=0o755)
    supply_answers(monkeypatch, ["agent-b", "http://100.101.102.103:8787"])
    previous = os.umask(0)
    try:
        run_setup(directory)
    finally:
        os.umask(previous)
    path = directory / "config.env"
    assert "AGENTMESH_AGENT_NAME=agent-b" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert list(directory.iterdir()) == [path]


def test_powershell_command_quotes_paths_literally(monkeypatch, tmp_path):
    directory = tmp_path / "person's $private folder"
    directory.mkdir()
    executable = directory / "agentmesh-mcp.exe"
    executable.touch()
    monkeypatch.setattr(sys, "argv", [str(executable.with_name("agentmesh-setup.exe"))])
    monkeypatch.setattr(sys, "platform", "win32")
    path = directory / "config.env"

    command = setup_wizard._registration_command(path)

    assert command.startswith("claude mcp add --scope user --transport stdio agentmesh --env ")
    assert "'AGENTMESH_CONFIG=" + str(path).replace("'", "''") + "'" in command
    assert command.endswith(" -- '" + str(executable).replace("'", "''") + "'")
    assert TOKEN not in command


@pytest.mark.parametrize("answer", ["", "n", "no", "anything else"])
def test_existing_file_is_unchanged_without_explicit_confirmation(monkeypatch, tmp_path, capsys, answer):
    path = tmp_path / "config.env"
    path.write_text("existing synthetic settings\n")
    path.chmod(0o640)
    supply_answers(monkeypatch, [answer], tokens=())

    run_setup(tmp_path)

    assert path.read_text() == "existing synthetic settings\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert "No changes made" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["y", "yes", "YES"])
def test_confirmed_replacement_is_private(monkeypatch, tmp_path, answer):
    path = tmp_path / "config.env"
    path.write_text("existing synthetic settings\n")
    supply_answers(monkeypatch, [answer, "agent-b", "https://example.com"])

    run_setup(tmp_path)

    assert "AGENTMESH_AGENT_NAME=agent-b" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("url", [
    "https://example.com", "https://machine.tailnet.ts.net:8443",
    "http://localhost:8787", "http://127.0.0.1:8787", "http://[::1]:8787",
    "http://100.64.0.0", "http://100.127.255.255:8787", "http://[fd7a:115c:a1e0::1]:8787",
])
def test_allowed_urls_match_client_policy(monkeypatch, tmp_path, url):
    assert is_allowed_url(url)
    supply_answers(monkeypatch, ["agent-a", url])
    run_setup(tmp_path)
    assert f"AGENTMESH_URL={url}\n" in (tmp_path / "config.env").read_text()


@pytest.mark.parametrize("url", [
    "http://example.com", "http://host.tailnet.ts.net", "http://192.168.1.1",
    "http://100.63.255.255", "http://100.128.0.0", "http://[fd7a:115c:a1e1::1]",
    "http://127.0.0.1@evil.example", "https:///missing-host", "https://example.com:99999",
    "ftp://localhost", "localhost:8787", "",
    "https://example.com/path\nAGENTMESH_TOKEN=injected",
])
def test_invalid_url_is_reprompted_before_token_entry(monkeypatch, tmp_path, capsys, url):
    supply_answers(monkeypatch, ["agent-a", url, "https://example.com"])
    run_setup(tmp_path)
    output = capsys.readouterr()
    assert "Use https://" in output.out
    assert TOKEN not in output.out + output.err
    assert (tmp_path / "config.env").read_text().startswith("AGENTMESH_URL=https://example.com\n")


def test_bad_agent_names_and_unsafe_tokens_are_reprompted(monkeypatch, tmp_path, capsys):
    supply_answers(monkeypatch, ["viewer", "agent-a\nAGENTMESH_URL=bad", "agent-b", "https://example.com"],
                   tokens=("", "two words", "'quoted'", "line\nbreak", "null\x00byte", TOKEN))
    run_setup(tmp_path)
    output = capsys.readouterr()
    assert "Choose agent-a or agent-b" in output.out
    assert "without spaces" in output.out
    assert TOKEN not in output.out + output.err
    assert (tmp_path / "config.env").read_text().endswith("AGENTMESH_AGENT_NAME=agent-b\n")


@pytest.mark.parametrize("exception", [EOFError, KeyboardInterrupt])
def test_interrupted_input_leaves_no_config(monkeypatch, tmp_path, capsys, exception):
    def cancel(prompt):
        raise exception

    monkeypatch.setattr("builtins.input", cancel)
    with pytest.raises(SystemExit) as error:
        run_setup(tmp_path)
    assert error.value.code == 1
    assert not (tmp_path / "config.env").exists()
    assert "No settings were saved" in capsys.readouterr().err


def test_token_entry_never_falls_back_to_visible_input(monkeypatch, tmp_path, capsys):
    import warnings

    def insecure_getpass(prompt):
        warnings.warn("Cannot hide input", getpass.GetPassWarning)
        raise AssertionError("Visible input must not be attempted")

    supply_answers(monkeypatch, ["agent-a", "https://example.com"])
    monkeypatch.setattr(getpass, "getpass", insecure_getpass)
    with pytest.raises(SystemExit) as error:
        run_setup(tmp_path)
    assert error.value.code == 1
    assert not (tmp_path / "config.env").exists()
    assert "interactive terminal" in capsys.readouterr().err


def test_symlink_config_is_not_followed_or_replaced(monkeypatch, tmp_path):
    target = tmp_path / "synthetic-original"
    target.write_text("leave unchanged")
    (tmp_path / "config.env").symlink_to(target)
    supply_answers(monkeypatch, [], tokens=())
    with pytest.raises(SystemExit):
        run_setup(tmp_path)
    assert target.read_text() == "leave unchanged"
    assert (tmp_path / "config.env").is_symlink()


def test_concurrent_creation_is_not_overwritten(monkeypatch, tmp_path):
    supply_answers(monkeypatch, ["agent-a", "https://example.com"])
    original_link = os.link

    def create_before_link(source, destination):
        Path(destination).write_text("another setup won the race")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", create_before_link)
    with pytest.raises(SystemExit):
        run_setup(tmp_path)
    assert (tmp_path / "config.env").read_text() == "another setup won the race"
    assert list(tmp_path.iterdir()) == [tmp_path / "config.env"]


def test_write_failure_does_not_leak_token_or_leave_temporary_file(monkeypatch, tmp_path, capsys):
    supply_answers(monkeypatch, ["agent-a", "https://example.com"])

    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(SystemExit):
        run_setup(tmp_path)
    assert list(tmp_path.iterdir()) == []
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert "Could not save settings" in output.err


def test_help_needs_no_input_or_settings(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    supply_answers(monkeypatch, [], tokens=())
    with pytest.raises(SystemExit) as error:
        setup_wizard.main(["--help"])
    assert error.value.code == 0
    assert list(tmp_path.iterdir()) == []
    assert "--config-dir" in capsys.readouterr().out
