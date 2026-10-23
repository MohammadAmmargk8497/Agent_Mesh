"""agentmesh-server init/serve entry points."""

import argparse
import signal
import sys
import threading

from .config import config_directory, init_config
from .http import create_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Self-host AgentMesh over Tailscale")
    parser.add_argument("--config-dir", help="config directory (or AGENTMESH_SERVER_DIR)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--config-dir", default=argparse.SUPPRESS,
                             help="config directory (or AGENTMESH_SERVER_DIR)")
        if name == "init":
            command.add_argument("--force", action="store_true", help="replace existing tokens")
        else:
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8787)
            command.add_argument("--allow-remote-bind", action="store_true",
                                 help="explicitly permit listening beyond loopback")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            config = init_config(args.config_dir, force=args.force)
            print(f"Created {config_directory(args.config_dir) / 'config'} (private; keep tokens secret).")
            print("Client config snippets (replace URL with your Tailscale HTTPS address):")
            for label, key in (("agent-a", "AGENT_A_TOKEN"), ("agent-b", "AGENT_B_TOKEN")):
                print(f"\n# {label}\nAGENTMESH_URL=http://localhost:8787\nAGENTMESH_TOKEN={config[key]}")
            print(f"\nViewer key (dashboard only): {config['VIEWER_TOKEN']}")
            return
        server = create_server(args.config_dir, args.host, args.port, args.allow_remote_bind)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"agentmesh-server: {exc}\n")

    def stop(signum, frame):
        # BaseServer.shutdown must run on a different thread from serve_forever.
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    host, port = server.server_address[:2]
    display_host = f"[{host}]" if ":" in host else host
    print(f"AgentMesh listening on http://{display_host}:{port}", flush=True)
    if args.allow_remote_bind:
        print("Warning: remote bind enabled; use a trusted network and firewall.", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
