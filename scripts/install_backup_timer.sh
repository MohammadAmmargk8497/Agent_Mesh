#!/usr/bin/env bash
# Install only when explicitly invoked; never read, source or print credentials.
set -euo pipefail
umask 077

project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="${HOME}/agentmesh-backups"
while (($#)); do
    case "$1" in
        --project|--backup-dir)
            if (($# < 2)); then printf 'Missing option value\n' >&2; exit 2; fi
            if [[ "$1" == --project ]]; then project="$2"; else backup_dir="$2"; fi
            shift 2
            ;;
        --help|-h)
            printf 'Usage: %s [--project PATH] [--backup-dir PATH]\n' "${0##*/}"
            exit 0
            ;;
        *) printf 'Unknown option\n' >&2; exit 2 ;;
    esac
done
if [[ "$project" != /* || "$backup_dir" != /* || ! -f "$project/scripts/backup_live.py" ]]; then
    printf 'Use absolute paths and a project containing scripts/backup_live.py\n' >&2
    exit 2
fi
# Systemd supports quoted arguments, with backslash escaping and %% for literal
# percent characters. Double $ prevents environment expansion in ExecStart.
unit_quote() {
    local value="$1"
    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
        printf 'Newlines are not allowed in paths\n' >&2; exit 2
    fi
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//%/%%}"
    value="${value//\$/\$\$}"
    printf '"%s"' "$value"
}
script_arg="$(unit_quote "$project/scripts/backup_live.py")"
backup_arg="$(unit_quote "$backup_dir")"
unit_dir="${HOME}/.config/systemd/user"
mkdir -p -- "$unit_dir"
cat > "$unit_dir/agentmesh-backup.service" <<EOF
[Unit]
Description=Preserve a verified paged AgentMesh backup

[Service]
Type=oneshot
UMask=0077
TimeoutStartSec=0
ExecStart=/usr/bin/env python3 $script_arg --backup-dir $backup_arg
EOF
cat > "$unit_dir/agentmesh-backup.timer" <<'EOF'
[Unit]
Description=Daily AgentMesh backup (all runs retained)

[Timer]
OnCalendar=daily
Persistent=true
Unit=agentmesh-backup.service

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now agentmesh-backup.timer
printf 'Installed daily AgentMesh backup timer. Configure the private ~/.config/agentmesh/tokens.env file before its first run.\n'
