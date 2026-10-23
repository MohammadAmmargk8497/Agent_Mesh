"""Optional, fail-open editor and post-commit helpers. No shell commands are used."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

MARKER = "# AgentMesh post-commit hook"
MEMBERS = ("agent-a", "agent-b")


def _settings_path(path: str) -> Path:
    if not path or re.search(r"[\x00-\x1f\x7f-\x9f]", path):
        raise ValueError("settings path must not contain control characters")
    target = Path(path).expanduser().absolute()
    for candidate in (target, *target.parents):
        if candidate.is_symlink():
            raise ValueError("refusing a symlink in the settings path")
    if target.exists() and not target.is_file():
        raise ValueError("settings path must be a regular file")
    if any(parent.exists() and not parent.is_dir() for parent in target.parents):
        raise ValueError("settings parent must be a directory")
    return target


def _settings_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("settings JSON must not contain duplicate keys")
        result[key] = value
    return result


def _invalid_settings_constant(_: str) -> None:
    raise ValueError("settings file must contain valid JSON constants")


def _read_settings(path: Path) -> tuple[dict[str, Any], bytes | None, int]:
    if not path.exists():
        return {}, None, 0o600
    # Refuse symlinks even if the file was replaced after the initial path check.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        mode = os.fstat(source.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("settings path must be a regular file")
        raw = source.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("settings file exceeds the 4 MiB safety limit")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_settings_object,
                          parse_constant=_invalid_settings_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("settings file must contain valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise TypeError("settings must be a JSON object")
    return data, raw, stat.S_IMODE(mode) & 0o777


def _hook_action(command: str) -> str | None:
    """Recognize documented standalone/module forms without executing commands."""
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    if args[:1] == ["&"]:  # PowerShell's literal executable invocation.
        args = args[1:]
    if not args:
        return None
    executable = args[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    if len(args) == 1:
        return {"agentmesh-notify": "notify", "agentmesh-guard": "guard"}.get(executable)
    if executable == "agentmesh-mcp" and args[1:] == ["before-return"]:
        return "before-return"
    if (re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", executable)
            and len(args) == 4 and args[1:3] == ["-m", "agentmesh_mcp.server"]
            and args[3] in ("notify", "guard", "before-return")):
        return args[3]
    return None


def _merged_hooks(settings: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged = copy.deepcopy(settings)
    if "hooks" in merged and not isinstance(merged["hooks"], dict):
        raise ValueError("settings hooks must be an object")
    installed = merged.setdefault("hooks", {})
    for groups in installed.values():
        if not isinstance(groups, list):
            raise TypeError("each hook event must contain a list")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise TypeError("each hook group must contain a hooks list")
            if "matcher" in group and not isinstance(group["matcher"], str):
                raise ValueError("hook matchers must be strings")
            for hook in group["hooks"]:
                if not isinstance(hook, dict) or not isinstance(hook.get("type"), str):
                    raise TypeError("each hook must be an object with a type")
                if hook["type"] == "command" and not isinstance(hook.get("command"), str):
                    raise ValueError("command hooks must contain a command string")
    windows = sys.platform == "win32"
    command = ("& '" + sys.executable.replace("'", "''") + "' -m agentmesh_mcp.server"
               if windows else shlex.join([sys.executable, "-m", "agentmesh_mcp.server"]))
    shell = {"shell": "powershell"} if windows else {}
    desired = {
        "UserPromptSubmit": {"hooks": [{"type": "command", "command": command + " notify", "timeout": 10, **shell}]},
        "PreToolUse": {"matcher": "Write|Edit|MultiEdit|NotebookEdit",
                       "hooks": [{"type": "command", "command": command + " guard", "timeout": 10, **shell}]},
        "Stop": {"hooks": [{"type": "command", "command": command + " before-return", "timeout": 30, **shell}]},
    }
    diff: list[dict[str, Any]] = []
    if "hooks" not in settings:
        additions = {event: [group] for event, group in desired.items()}
        diff.append({"op": "add", "path": "/hooks", "value": additions})
        installed.update(additions)
        return merged, diff
    for event, group in desired.items():
        hook = group["hooks"][0]
        groups = installed.setdefault(event, [])
        action = {"UserPromptSubmit": "notify", "PreToolUse": "guard", "Stop": "before-return"}[event]
        if any(set(existing.get("matcher", "").split("|")) == set(group.get("matcher", "").split("|"))
               and any(item.get("type") == "command" and (item.get("command") == hook["command"]
                       or _hook_action(item["command"]) == action)
                       for item in existing["hooks"]) for existing in groups):
            continue
        groups.append(group)
        diff.append({"op": "add", "path": f"/hooks/{event}" + ("/-" if event in settings["hooks"] else ""),
                     "value": group if event in settings["hooks"] else [group]})
    return merged, diff


def _write_settings(path: Path, data: dict[str, Any], original: bytes | None, mode: int) -> str | None:
    _settings_path(str(path))
    if _read_settings(path)[1] != original:
        raise ValueError("settings changed during installation; rerun to review a fresh diff")
    for parent in reversed(path.parents):
        if not parent.exists():
            parent.mkdir(mode=0o700)
    backup: str | None = None
    if original is not None:
        descriptor, backup = tempfile.mkstemp(prefix=path.name + ".agentmesh-backup-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(original)
            destination.flush()
            os.fsync(destination.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".agentmesh-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(data, destination, indent=2, ensure_ascii=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
            os.chmod(temporary, mode)
        _settings_path(str(path))
        if _read_settings(path)[1] != original:
            raise ValueError("settings changed during installation; rerun to review a fresh diff")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def install_hooks(argv: list[str] | None = None) -> None:
    """Preview additive Claude Code hooks; only --apply writes settings and a backup."""
    parser = argparse.ArgumentParser(prog="agentmesh-mcp install-hooks", allow_abbrev=False)
    parser.add_argument("--apply", action="store_true", help="back up and update the settings file")
    parser.add_argument("--settings", default="~/.claude/settings.json", help="Claude Code settings JSON path")
    args = parser.parse_args(argv)
    try:
        path = _settings_path(args.settings)
        settings, original, mode = _read_settings(path)
        merged, diff = _merged_hooks(settings)
        backup = _write_settings(path, merged, original, mode) if args.apply and diff else None
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    except OSError:
        parser.error("could not read or write the Claude settings file")
    print(json.dumps({"settings": str(path), "dry_run": not args.apply, "changed": bool(diff),
                      "applied": bool(args.apply and diff), "backup": backup, "diff": diff}, indent=2))


def _api(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    # Lazy import: a non-editing tool does not need to load the client's configuration.
    from .server import api

    return api(method, path, payload, timeout=4)


def _root(directory: Path) -> Path:
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ValueError("not a repository")


def guard() -> None:
    """Claude Code PreToolUse: ask (or deny) before editing another member's claimed file."""
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            return
        data = event.get("tool_input", {})
        path = data.get("file_path") or data.get("notebook_path")
        if not isinstance(path, str) or not path or re.search(r"[\x00-\x1f\x7f-\x9f]", path):
            return
        cwd = Path(event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
        root = _root(cwd)
        candidate = Path(path)
        relative = (candidate if candidate.is_absolute() else cwd / candidate).resolve().relative_to(root).as_posix()
        result = _api("GET", f"/v1/claims?{httpx.QueryParams(path=relative)}")
        # The authenticated identity returned by this new route is authoritative;
        # configured display names must not bypass another member's file claim.
        identity = result.get("agent")
        if identity not in MEMBERS:
            return
        for claim in result.get("claims", []):
            holder = claim.get("agent")
            if holder not in MEMBERS or holder == identity:
                continue
            mode = "deny" if os.environ.get("AGENTMESH_CLAIM_MODE") == "deny" else "ask"
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": mode,
                              "permissionDecisionReason": f"AgentMesh: {holder} holds this file. Coordinate with them or wait for the claim to expire before editing."}}))
            return
    except Exception:  # noqa: BLE001 - optional editing guards must fail open on any error.
        # A disconnected hub must not block local editing.
        return


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True, shell=False, timeout=5)
    return result.stdout.rstrip("\r\n")


def _install() -> None:
    # git-path supports ordinary repositories and linked worktrees without
    # assuming that .git is a directory.
    hook = Path(_git("rev-parse", "--git-path", "hooks/post-commit"))
    if hook.is_symlink() or (hook.exists() and MARKER not in hook.read_text()):
        print("AgentMesh: refusing to overwrite an existing post-commit hook.", file=sys.stderr)
        return
    hook.parent.mkdir(parents=True, exist_ok=True)
    command = shlex.quote(sys.executable) + " -m agentmesh_mcp.hooks"
    hook.write_text("#!/bin/sh\n" + MARKER + "\n" + command + " >/dev/null 2>&1 || :\nexit 0\n")
    hook.chmod(0o755)
    print("AgentMesh: installed post-commit announcer.")


def commit_hook(argv: list[str] | None = None) -> None:
    """Install or run a best-effort post-commit announcement, never failing a commit."""
    try:
        args = sys.argv[1:] if argv is None else argv
        if args == ["--install"]:
            _install()
        elif not args:
            payload: dict[str, Any] = {"sha": _git("rev-parse", "HEAD"),
                                       "subject": _git("log", "-1", "--format=%s"),
                                       "branch": _git("rev-parse", "--abbrev-ref", "HEAD")}
            # NUL separators preserve spaces and newlines without shell parsing.
            files = _git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", "HEAD")
            payload["files"] = [path for path in files.split("\0") if path][:50]
            _api("POST", "/v1/commits", payload)
    except Exception:  # noqa: BLE001 - an optional announcer must never fail a commit.
        return


if __name__ == "__main__":
    commit_hook()
