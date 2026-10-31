#!/usr/bin/env bash
# Run an already-provisioned, dedicated staging tunnel. Never manage DNS/routes.
set -euo pipefail
umask 077

tunnel_id=""
credentials_file=""
while (($#)); do
    case "$1" in
        --tunnel-id|--credentials-file)
            if (($# < 2)) || [[ -z "$2" ]]; then
                printf 'Missing option value\n' >&2; exit 2
            fi
            if [[ "$1" == --tunnel-id ]]; then tunnel_id="$2"; else credentials_file="$2"; fi
            shift 2
            ;;
        --help|-h)
            printf 'Usage: %s --tunnel-id UUID [--credentials-file ABSOLUTE_PATH]\n' "${0##*/}"
            printf 'Runs only agentmesh-home.zahid.win -> http://127.0.0.1:8787. No DNS/routes are changed.\n'
            exit 0
            ;;
        *) printf 'Unknown option\n' >&2; exit 2 ;;
    esac
done
if [[ ! "$tunnel_id" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    printf 'Supply the UUID of a dedicated, locally managed staging tunnel\n' >&2
    exit 2
fi
if ! command -v cloudflared >/dev/null 2>&1; then
    printf 'Install cloudflared before starting the staging tunnel\n' >&2
    exit 2
fi

# Inherited token/URL/config flags must not replace this local configuration
# with a remotely managed tunnel or another origin. Never print their values.
for setting in ${!TUNNEL_@}; do unset "$setting"; done

config_file="$(python3 - "$tunnel_id" "$credentials_file" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def fail(message):
    print(f"home_tunnel: {message}", file=sys.stderr)
    raise SystemExit(2)


try:
    tunnel_id = sys.argv[1].lower()
    directory = Path.home() / ".cloudflared"
    if directory.is_symlink() or not directory.is_dir():
        fail("~/.cloudflared must be a private, non-symlink directory")
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        fail("~/.cloudflared must be owned by you with permissions 0700")
    credentials = Path(sys.argv[2]) if sys.argv[2] else directory / f"{tunnel_id}.json"
    if not credentials.is_absolute() or credentials.is_symlink() or not credentials.is_file():
        fail("credentials must be an existing, absolute, non-symlink file under ~/.cloudflared")
    credentials = credentials.resolve(strict=True)
    if not credentials.is_relative_to(directory.resolve(strict=True)):
        fail("credentials must remain under ~/.cloudflared")
    info = credentials.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or not os.access(credentials, os.R_OK):
        fail("credentials must be readable, owned by you, with permissions 0600 or stricter")
    # Only inspect file metadata: credential contents are read by cloudflared,
    # never by this script or written into the generated configuration.
    config = directory / "agentmesh-home.yml"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        fail("the dedicated agentmesh-home.yml must not be a symlink or special file")
    contents = (
        f"tunnel: {tunnel_id}\n"
        f"credentials-file: {json.dumps(str(credentials))}\n"
        "ingress:\n"
        "  - hostname: agentmesh-home.zahid.win\n"
        "    service: http://127.0.0.1:8787\n"
        "  - service: http_status:404\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                     prefix=".agentmesh-home-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, config)
        finally:
            temporary.unlink(missing_ok=True)
    print(config)
except OSError:
    fail("cannot prepare the private staging configuration; check file paths and permissions")
PY
)"
exec cloudflared tunnel --config "$config_file" run "${tunnel_id,,}"
