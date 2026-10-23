"""Guide a person through local client configuration without contacting a server."""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import sys
import sysconfig
import tempfile
import warnings
from pathlib import Path

from .url_policy import is_allowed_url


def _write_config(path: Path, agent: str, url: str, token: str, *, overwrite: bool) -> None:
    """Publish a complete owner-only file; never follow a config symlink."""
    directory = path.parent
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("The settings folder and file must not be symbolic links.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            os.chmod(temporary, 0o600)
            stream.write(f"AGENTMESH_URL={url}\nAGENTMESH_TOKEN={token}\nAGENTMESH_AGENT_NAME={agent}\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Unlike replace(), link() also refuses files created after the prompt.
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _registration_command(path: Path) -> str:
    windows = sys.platform == "win32"
    executable_name = "agentmesh-mcp.exe" if windows else "agentmesh-mcp"
    executable = Path(sys.argv[0]).absolute().with_name(executable_name)
    if not executable.is_file():
        executable = Path(sysconfig.get_path("scripts")) / executable_name
    args = ["claude", "mcp", "add", "--scope", "user", "--transport", "stdio", "agentmesh",
            "--env", f"AGENTMESH_CONFIG={path}", "--", str(executable)]
    if windows:
        # PowerShell single-quoted arguments are literal, including $ and spaces.
        return " ".join("'" + arg.replace("'", "''") + "'" if index in (9, 11) else arg
                        for index, arg in enumerate(args))
    return shlex.join(args)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Set up AgentMesh on this computer. No network connection is made.")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config" / "agentmesh",
                        help="where to save config.env (default: ~/.config/agentmesh)")
    args = parser.parse_args(argv)
    path = args.config_dir.expanduser().absolute() / "config.env"
    print("AgentMesh setup\nYour token is a private password. It will not appear as you type.")
    try:
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("The settings folder and file must not be symbolic links.")
        overwrite = path.exists()
        if overwrite:
            answer = input("Settings already exist. Replace them? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("No changes made. Your existing settings are unchanged.")
                return

        while True:
            agent = input("Your agent name (agent-a or agent-b): ").strip()
            if agent in ("agent-a", "agent-b"):
                break
            print("Choose agent-a or agent-b. Ask your host which one belongs to you.")

        while True:
            url = input("Server address (including https:// or http://): ").strip().rstrip("/")
            if is_allowed_url(url) and not any(character.isspace() or ord(character) < 32
                                                   for character in url):
                break
            print("Use https:// followed by the server address. Plain http:// is allowed only for "
                  "localhost or a loopback/Tailscale IP address, not a public address or .ts.net name.")

        while True:
            # Refuse getpass's visible-input fallback rather than risking a token leak.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("Your agent token (hidden): ")
            if token and token.strip("'\"") == token and not any(
                    character.isspace() or ord(character) < 32 for character in token):
                break
            print("Enter the token without spaces, line breaks, or surrounding quotes.")

        _write_config(path, agent, url, token, overwrite=overwrite)
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "\nSetup cancelled. No settings were saved.\n")
    except getpass.GetPassWarning:
        parser.exit(1, "Hidden token entry needs an interactive terminal. Run setup there instead.\n")
    except FileExistsError:
        parser.exit(1, "Settings already exist. Run setup again to confirm replacement.\n")
    except (OSError, ValueError):
        parser.exit(1, "Could not save settings. Check folder permissions and avoid symbolic links.\n")

    print(f"\nSaved your settings to {path}")
    print("No connection was tested and Claude Code has not been changed.")
    print("Run this command in " + ("PowerShell:" if sys.platform == "win32" else "your terminal:"))
    print(_registration_command(path))
    print("Then restart Claude Code and enter /mcp to check the connection.")


if __name__ == "__main__":
    main()
