"""Run the read-only count verifier directly from a source checkout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "src"))

from agentmesh_server.scripts.verify_export import main

if __name__ == "__main__":
    raise SystemExit(main())
