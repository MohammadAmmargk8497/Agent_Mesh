"""AgentMesh configuration boundary for the unmodified OpenHuddle client."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

VENDOR = Path(__file__).with_name("vendor") / "openhuddle"


def load_settings() -> tuple[Path, dict[str, str]]:
    """Keep the existing config.env format without importing the legacy MCP server."""
    path = Path(os.environ.get("AGENTMESH_CONFIG", "~/.config/agentmesh/config.env")).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("AgentMesh settings must not use symbolic links")
    cfg: dict[str, str] = {}
    if path.exists():
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("AgentMesh settings must be a regular file")
            if mode & 0o077:
                print("AgentMesh warning: settings are readable by other users; use chmod 600.", file=sys.stderr)
            for line in stream:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    cfg[key.strip()] = value.strip().strip("'\"")
    cfg.update({key: value for key, value in os.environ.items() if key.startswith("AGENTMESH_")})
    return path, cfg


def find_node(cfg: dict[str, str]) -> str:
    """Services may not inherit nvm's PATH; always enforce the client prerequisite."""
    candidates = [cfg.get("AGENTMESH_NODE"), shutil.which("node")]
    nvm = Path.home() / ".nvm/versions/node"
    candidates += [str(path) for path in sorted(nvm.glob("*/bin/node"), reverse=True)]
    for candidate in dict.fromkeys(item for item in candidates if item):
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True,
                                    check=False, timeout=5)
            match = re.fullmatch(r"v?(\d+)\.\d+\.\d+\s*", result.stdout)
            if result.returncode == 0 and match and int(match[1]) >= 20:
                return shutil.which(candidate) or candidate
        except (OSError, subprocess.SubprocessError):
            pass
    raise ValueError("AgentMesh requires Node.js >= 20 for agentmesh and agentmesh-mcp; install Node or set AGENTMESH_NODE. No legacy fallback was started.")


def _launch(mode: str, argv: list[str] | None = None) -> None:
    try:
        path, cfg = load_settings()
        node = find_node(cfg)
        env = os.environ.copy()
        env.update({key: value for key, value in cfg.items() if key.startswith("AGENTMESH_")})
        env["AGENTMESH_CONFIG"] = str(path)
        env.setdefault("OPENHUDDLE_HOME", str(path.parent / "openhuddle"))
        if cfg.get("AGENTMESH_URL"):
            env.setdefault("OPENHUDDLE_HUB", cfg["AGENTMESH_URL"])
        os.execve(node, [node, str(VENDOR / "agentmesh-client-boot.js"), mode,
                          *(sys.argv[1:] if argv is None else argv)], env)
    except (OSError, ValueError) as error:
        # Never include config contents or an exception from an HTTP response.
        message = str(error) if isinstance(error, ValueError) else "Could not launch the AgentMesh Node client."
        print(message, file=sys.stderr)
        raise SystemExit(1) from None


def main() -> None:
    _launch("mcp")


def cli() -> None:
    _launch("cli")


if __name__ == "__main__":
    main()
