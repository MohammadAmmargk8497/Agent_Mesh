"""The unified dashboard's actual inline scripts work against both hub runtimes."""

import json
import subprocess
from pathlib import Path

from agentmesh_server.config import init_config
from test_hub_seed import running_hub


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "cloudflare/test/unified_dashboard.cjs"


def run_dashboard(*arguments):
    result = subprocess.run(
        ["node", str(HARNESS), *arguments], cwd=ROOT,
        text=True, capture_output=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unified_dashboard_worker_controls():
    run_dashboard()


def test_unified_dashboard_python_controls(tmp_path):
    directory = tmp_path / "unified-hub"
    config = init_config(directory)
    with running_hub(directory, config) as api:
        run_dashboard(json.dumps({
            "origin": f"http://{api.host}:{api.port}", "tokens": api.tokens,
        }))
