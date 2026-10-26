"""Exercise the Docker backup command against real, live SQLite databases."""

import os
import signal
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sqlite-backup.sh"
DATABASES = ("data.db", "hub.db")


def create_database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
    connection.execute("INSERT INTO records VALUES (?)", (f"committed-{path.name}",))
    connection.commit()
    return connection


def run_backup(data_directory, backup_directory, interval="--once"):
    return subprocess.run(
        ["sh", str(SCRIPT), interval, str(data_directory), str(backup_directory)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def snapshots(directory):
    return sorted(path for path in directory.iterdir() if not path.name.startswith("."))


@pytest.fixture
def live_databases(tmp_path):
    data_directory = tmp_path / "live data"
    data_directory.mkdir()
    connections = {
        name: create_database(data_directory / name) for name in DATABASES
    }
    try:
        yield data_directory, connections
    finally:
        for connection in connections.values():
            connection.close()


def test_backup_reads_live_wal_and_publishes_private_independent_copies(live_databases, tmp_path):
    data_directory, connections = live_databases
    backup_directory = tmp_path / "saved backups"
    for name, connection in connections.items():
        assert (data_directory / f"{name}-wal").stat().st_size > 0
        # An in-flight write must not leak into the consistent, committed backup.
        connection.execute("INSERT INTO records VALUES ('uncommitted')")

    result = run_backup(data_directory, backup_directory)

    assert result.returncode == 0, result.stderr
    [snapshot] = snapshots(backup_directory)
    assert set(snapshot.iterdir()) == {snapshot / name for name in DATABASES}
    assert stat.S_IMODE(backup_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
    assert not list(backup_directory.glob(".pending-*"))
    for name, connection in connections.items():
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        connection.rollback()
        connection.execute("DELETE FROM records")
        connection.execute("INSERT INTO records VALUES ('changed after backup')")
        connection.commit()
        copy = snapshot / name
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        with sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True) as saved:
            assert saved.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert saved.execute("SELECT value FROM records").fetchall() == [
                (f"committed-{name}",)
            ]


def test_backup_missing_hub_does_not_create_source_or_snapshot(tmp_path):
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    create_database(data_directory / "data.db").close()
    backup_directory = tmp_path / "backups"

    result = run_backup(data_directory, backup_directory)

    assert result.returncode != 0
    assert not (data_directory / "hub.db").exists()
    assert not backup_directory.exists()
    assert "Waiting for data.db and hub.db" in result.stderr


def test_corrupt_hub_removes_partial_snapshot(tmp_path):
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    create_database(data_directory / "data.db").close()
    invalid_database = b"This is not a SQLite database.\n" * 20
    (data_directory / "hub.db").write_bytes(invalid_database)
    backup_directory = tmp_path / "backups"

    result = run_backup(data_directory, backup_directory)

    assert result.returncode != 0
    assert list(backup_directory.iterdir()) == []
    assert (data_directory / "hub.db").read_bytes() == invalid_database


def test_repeated_backups_have_distinct_complete_snapshot_names(live_databases, tmp_path):
    data_directory, _ = live_databases
    backup_directory = tmp_path / "backups"
    for _ in range(2):
        result = run_backup(data_directory, backup_directory)
        assert result.returncode == 0, result.stderr
    copies = snapshots(backup_directory)
    assert len(copies) == 2
    assert copies[0].name != copies[1].name
    for snapshot in copies:
        assert {path.name for path in snapshot.iterdir()} == set(DATABASES)


@pytest.mark.parametrize("interval", ["", "0", "00", "-1", "1.5", "invalid"])
def test_invalid_interval_is_rejected_without_creating_files(tmp_path, interval):
    data_directory = tmp_path / "data"
    backup_directory = tmp_path / "backups"

    result = run_backup(data_directory, backup_directory, interval)

    assert result.returncode == 2
    assert "positive interval" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_scheduled_backups_repeat_and_stop_on_term(live_databases, tmp_path):
    data_directory, _ = live_databases
    backup_directory = tmp_path / "backups"
    process = subprocess.Popen(
        ["sh", str(SCRIPT), "1", str(data_directory), str(backup_directory)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if backup_directory.exists() and len(snapshots(backup_directory)) >= 2:
                break
            assert process.poll() is None, "Scheduled backup exited unexpectedly"
            time.sleep(0.05)
        assert len(snapshots(backup_directory)) >= 2
        assert not list(backup_directory.glob(".pending-*"))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)
            raise
    assert process.returncode == 0, stderr
    assert stdout.count("SQLite backup complete:") >= 2
