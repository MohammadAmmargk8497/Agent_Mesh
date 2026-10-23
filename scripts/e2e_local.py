"""Run the unchanged e2e test against a disposable, loopback-only Python server."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    # Keep all generated credentials and database files inside the workspace.
    with tempfile.TemporaryDirectory(prefix=".agentmesh-e2e-", dir=ROOT) as directory:
        config_dir = Path(directory) / "config"
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(ROOT / "server" / "src"), env.get("PYTHONPATH", ""),
        )))
        # Never send fixture bearer tokens through an inherited HTTP proxy.
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
        command = [sys.executable, "-m", "agentmesh_server"]
        subprocess.run(
            [*command, "init", "--config-dir", str(config_dir)],
            cwd=ROOT, env=env, check=True, capture_output=True,
        )
        config = json.loads((config_dir / "config").read_text())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        url = f"http://127.0.0.1:{port}"
        local_http = build_opener(ProxyHandler({}))
        # Never forward captured init output: it contains the temporary tokens.
        with (Path(directory) / "server.log").open("wb") as log:
            server = subprocess.Popen(
                [*command, "serve", "--config-dir", str(config_dir),
                 "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 15
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("local AgentMesh server exited before becoming ready")
                    try:
                        with local_http.open(f"{url}/health", timeout=1) as response:
                            if response.status == 200:
                                break
                    except (URLError, TimeoutError):
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("local AgentMesh server did not become ready within 15 seconds")
                    time.sleep(0.05)

                env.update({
                    "AGENTMESH_URL": url,
                    "A_TOKEN": config["AGENT_A_TOKEN"].split(",")[0].strip(),
                    "B_TOKEN": config["AGENT_B_TOKEN"].split(",")[0].strip(),
                })
                env.pop("AGENTMESH_CF_ACCESS_CLIENT_ID", None)
                env.pop("AGENTMESH_CF_ACCESS_CLIENT_SECRET", None)
                return subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "e2e_test.py")],
                    cwd=ROOT, env=env, check=False,
                ).returncode
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
