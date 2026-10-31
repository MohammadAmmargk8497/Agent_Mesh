"""Run the real Worker cookie-auth boundary offline against SQLite."""

import subprocess
from pathlib import Path


def test_worker_cookie_authentication_and_csrf():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", str(root / "cloudflare/test/cookie_auth.cjs")],
        cwd=root, capture_output=True, text=True, timeout=45, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
