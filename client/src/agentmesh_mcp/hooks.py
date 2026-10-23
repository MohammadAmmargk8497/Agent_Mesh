"""Optional, fail-open editor and post-commit helpers. No shell commands are used."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

MARKER = "# AgentMesh post-commit hook"
MEMBERS = ("agent-a", "agent-b")


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
