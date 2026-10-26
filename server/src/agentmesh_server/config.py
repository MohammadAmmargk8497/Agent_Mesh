"""Private on-disk configuration, environment bootstrap and rotatable tokens."""

import json
import os
from pathlib import Path
import secrets
import tempfile

TOKEN_KEYS = ("AGENT_A_TOKEN", "AGENT_B_TOKEN", "VIEWER_TOKEN")
AGENT_TOKEN_KEYS = TOKEN_KEYS[:2]


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
    """Prefer a complete agent-token environment over the existing local config."""
    # Either environment key opts into unattended/container mode. Never mix an
    # incomplete environment with disk credentials or persist raw env tokens.
    if any(key in os.environ for key in AGENT_TOKEN_KEYS):
        config = {key: os.environ.get(key) for key in AGENT_TOKEN_KEYS}
        _validate_tokens(config, AGENT_TOKEN_KEYS)
        agent_a = {token.strip() for token in config[AGENT_TOKEN_KEYS[0]].split(",")}
        agent_b = {token.strip() for token in config[AGENT_TOKEN_KEYS[1]].split(",")}
        if agent_a & agent_b:
            raise ValueError("AGENT_A_TOKEN and AGENT_B_TOKEN must use distinct tokens")
        return config
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
    _validate_tokens(config, TOKEN_KEYS)
    path.chmod(0o600)
    return config


def _validate_tokens(config, keys):
    for key in keys:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip() or any(
                len(token.strip()) < 32 for token in value.split(",")):
            raise ValueError(f"{key} must contain comma-separated tokens of at least 32 characters")
