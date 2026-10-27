"""Run actual Worker owner-export routes offline against SQLite."""

import subprocess
from pathlib import Path


def test_worker_export_paging_and_owner_gate():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", "-e", "require('./cloudflare/test/export.cjs')().catch(e => { console.error(e); process.exitCode = 1; })"],
        cwd=root, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
