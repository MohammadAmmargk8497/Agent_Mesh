"""Private on-disk configuration and rotatable bearer tokens."""

import json
import os
from pathlib import Path
import secrets
import tempfile

TOKEN_KEYS = ("AGENT_A_TOKEN", "AGENT_B_TOKEN", "VIEWER_TOKEN")


def config_directory(config_dir=None):
    return Path(config_dir or os.environ.get("AGENTMESH_SERVER_DIR") or
                Path.home() / ".config" / "agentmesh-server").expanduser()


def secure_directory(config_dir):
    directory = config_directory(config_dir)
    if directory.is_symlink():
        raise ValueError("config directory must not be a symbolic link")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def init_config(config_dir=None, force=False):
    """Generate a private JSON config; replacement requires an explicit force."""
    directory = secure_directory(config_dir)
    path = directory / "config"
    if path.is_symlink():
        raise ValueError("config must not be a symbolic link")
    if path.exists() and not force:
        raise FileExistsError("config already exists; use --force to replace its tokens")
    config = {key: secrets.token_urlsafe(32) for key in TOKEN_KEYS}
    # A temporary file ensures neither partial JSON nor permissive token files
    # are exposed. Link-without-replacement also makes concurrent init safe.
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(config, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return config


def load_config(config_dir=None):
    directory = secure_directory(config_dir)
    path = directory / "config"
    if path.is_symlink():
        raise ValueError("config must not be a symbolic link")
    try:
        with path.open(encoding="utf-8") as stream:
            config = json.load(stream)
    except FileNotFoundError:
        raise ValueError("config not found; run agentmesh-server init first") from None
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    for key in TOKEN_KEYS:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip() or any(
                len(token.strip()) < 32 for token in value.split(",")):
            raise ValueError(f"{key} must contain comma-separated tokens of at least 32 characters")
    path.chmod(0o600)
    return config
