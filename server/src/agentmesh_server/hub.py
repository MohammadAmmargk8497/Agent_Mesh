"""Lazy, private loopback process boundary for the vendored OpenHuddle hub."""

import hashlib
import json
import logging
import os
import re
import select
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .config import secure_directory

logger = logging.getLogger(__name__)


class HubUnavailable(Exception):
    """The optional hub could not start; the legacy API remains available."""


def find_node():
    """Services may not inherit nvm's PATH; prefer an explicit node override."""
    override = os.environ.get("AGENTMESH_NODE")
    if override:
        return override
    found = shutil.which("node")
    if found:
        return found
    candidates = []
    for path in (Path.home() / ".nvm" / "versions" / "node").glob("*/bin/node"):
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", path.parents[1].name)
        if match and path.is_file():
            candidates.append((tuple(map(int, match.groups())), str(path)))
    return max(candidates, default=((), None))[1]


def resolve_public_url(explicit, host, port):
    value = explicit or os.environ.get("AGENTMESH_PUBLIC_URL")
    if value:
        return value.rstrip("/")
    tailscale = shutil.which("tailscale")
    if tailscale:
        try:
            result = subprocess.run([tailscale, "status", "--json"], capture_output=True,
                                    timeout=2, check=True)
            dns_name = json.loads(result.stdout).get("Self", {}).get("DNSName", "").rstrip(".")
            if dns_name and re.fullmatch(r"[A-Za-z0-9.-]+", dns_name):
                return f"https://{dns_name}"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


class HubSupervisor:
    """Start only for /api, send hashes over stdin, and never expose child logs."""

    def __init__(self, config_dir, config, host, port, *, enabled=True, public_url=None):
        self.directory = secure_directory(config_dir)
        self.hashes = {name: hashlib.sha256(config[key].split(",")[0].strip().encode()).hexdigest()
                       for name, key in (("agent-a", "AGENT_A_TOKEN"), ("agent-b", "AGENT_B_TOKEN"))}
        self.host, self.port = host, port
        self.explicit_public_url = public_url
        self.enabled = enabled
        self.lock = threading.RLock()
        self.process = None
        self.url = None
        self._public_url = None
        self.node = None
        self.checked = False
        self.failed = False
        self.closed = False

    def _unavailable(self, reason="hub requires a working Node.js >= 22.13 (set AGENTMESH_NODE if needed)"):
        if not self.failed:
            logger.error("AgentMesh /api unavailable: %s; /v1 remains available", reason)
        self.failed = True
        raise HubUnavailable("hub unavailable")

    def _database_path(self):
        path = self.directory / "hub.db"
        for suffix in ("", "-wal", "-shm"):
            if Path(str(path) + suffix).is_symlink():
                raise ValueError("hub database must not be a symbolic link")
        return path

    def available(self):
        with self.lock:
            if not self.enabled or self.closed or self.failed:
                raise HubUnavailable("hub unavailable")
            try:
                self._database_path()
            except (OSError, ValueError):
                self._unavailable("hub database path is unsafe (symbolic links are refused)")
            if self.process is not None and self.process.poll() is not None:
                self._unavailable()
            if not self.checked:
                self.checked = True
                self.node = find_node()
                try:
                    if not self.node:
                        self._unavailable()
                    result = subprocess.run([self.node, "--version"], capture_output=True,
                                            timeout=5, check=True, env=self._child_env())
                    match = re.fullmatch(rb"v(\d+)\.(\d+)\.\d+\s*", result.stdout)
                    if not match or tuple(map(int, match.groups())) < (22, 13):
                        self._unavailable()
                except (OSError, subprocess.SubprocessError):
                    self._unavailable()

    @staticmethod
    def _child_env():
        # Deliberately do not inherit credentials, NODE_OPTIONS or loader hooks.
        return {key: os.environ[key] for key in ("PATH", "HOME", "SYSTEMROOT") if key in os.environ}

    @property
    def public_url(self):
        with self.lock:
            if self._public_url is None:
                self._public_url = resolve_public_url(self.explicit_public_url, self.host, self.port)
            return self._public_url

    def endpoint(self):
        with self.lock:
            self.available()
            if self.url:
                return urlsplit(self.url)
            try:
                # Recheck at the open boundary, even after page/API preflight.
                path = self._database_path()
                fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
                boot = Path(__file__).parent / "vendor" / "openhuddle" / "agentmesh-boot.js"
                self.process = subprocess.Popen(
                    [self.node, str(boot)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=self._child_env(), cwd=self.directory,
                )
                options = {"dbPath": str(path.resolve()), "publicUrl": self.public_url,
                           "tokenHashes": self.hashes}
                self.process.stdin.write((json.dumps(options) + "\n").encode())
                self.process.stdin.close()
                ready, _, _ = select.select([self.process.stdout], [], [], 10)
                if not ready:
                    raise ValueError("hub startup timed out")
                result = json.loads(self.process.stdout.readline(65536))
                endpoint = urlsplit(result["url"])
                if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
                    raise ValueError("invalid hub endpoint")
                self.url = result["url"]
                return endpoint
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                self._stop()
                self._unavailable("hub could not start safely")

    def _stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def close(self):
        with self.lock:
            self.closed = True
            self._stop()
