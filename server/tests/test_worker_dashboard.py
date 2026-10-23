"""Execute the Worker's actual routes against Node's built-in SQLite runtime."""

import subprocess
from pathlib import Path


def test_worker_dashboard_routes():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", str(root / "server/tests/worker_dashboard.cjs")],
        cwd=root, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
