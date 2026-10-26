#!/bin/sh
# Usage: agentmesh-backup [--once|interval-seconds] [data-dir] [backup-dir]
set -eu
umask 077

interval=${1-86400}
data_dir=${2:-/data}
backup_dir=${3:-/backups}
case "$interval" in
    --once) ;;
    ''|*[!0-9]*|0) echo 'Expected --once or a positive interval in seconds' >&2; exit 2 ;;
esac
if [ "$interval" != --once ] && ! [ "$interval" -gt 0 ] 2>/dev/null; then
    echo 'Expected --once or a positive interval in seconds' >&2
    exit 2
fi

pending=
cleanup() {
    if [ -n "$pending" ]; then
        rm -rf -- "$pending"
    fi
}
trap cleanup EXIT
trap 'exit 0' INT TERM

snapshot() {
    # hub.db is lazy: never create an empty source database just to back it up.
    if [ ! -f "$data_dir/data.db" ] || [ ! -f "$data_dir/hub.db" ]; then
        echo 'Waiting for data.db and hub.db (the hub starts on first /api use)' >&2
        return 1
    fi
    mkdir -p "$backup_dir" || return 1
    pending=$(mktemp -d "$backup_dir/.pending-XXXXXXXX") || return 1
    for db in data.db hub.db; do
        if ! sqlite3 -readonly "$data_dir/$db" '.timeout 10000' ".backup '$pending/$db'"; then
            cleanup
            pending=
            return 1
        fi
        # A WAL-mode source carries its journal mode into the copy. Finalize
        # only the copy so restores need neither live sidecars nor a WAL writer.
        if [ "$(sqlite3 "$pending/$db" 'PRAGMA journal_mode=DELETE;')" != delete ]; then
            echo "Backup finalization failed for $db" >&2
            cleanup
            pending=
            return 1
        fi
        if [ "$(sqlite3 -readonly "$pending/$db" 'PRAGMA quick_check;')" != ok ]; then
            echo "Backup integrity check failed for $db" >&2
            cleanup
            pending=
            return 1
        fi
    done
    destination="$backup_dir/$(date -u +%Y%m%dT%H%M%SZ)-${pending##*.pending-}"
    if ! mv -- "$pending" "$destination"; then
        cleanup
        pending=
        return 1
    fi
    pending=
    echo "SQLite backup complete: $destination"
}

if [ "$interval" = --once ]; then
    snapshot
else
    while :; do
        if ! snapshot; then
            echo 'Backup not completed; retrying at the next interval' >&2
        fi
        sleep "$interval" &
        wait "$!" || :
    done
fi
