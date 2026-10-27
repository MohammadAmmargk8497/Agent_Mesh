"""Exercise every former hub history sweep on the Python-hosted implementation."""

import subprocess
from pathlib import Path


def test_python_hosted_hub_keeps_all_backdated_history():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", str(root / "cloudflare/test/history.cjs"), "--server"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
