"""Offline, real-Worker-export migration tests; no production APIs or credentials."""

import copy
import io
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from agentmesh_server.scripts._sqlite_export import (
    DATABASES,
    ExportError,
    decode_value,
    quote,
)
from agentmesh_server.scripts.import_export import import_export
from agentmesh_server.scripts.verify_export import verify_export

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def worker_export():
    # This harness calls the actual owner-only Worker route with populated DO
    # adapters, so the Python consumer is tested against the real producer.
    result = subprocess.run(["node", "cloudflare/test/export.cjs", "--fixture"],
                            cwd=ROOT, capture_output=True, text=True, check=True, timeout=40)
    return json.loads(result.stdout)


@pytest.fixture
def export_path(tmp_path, worker_export):
    path = tmp_path / "owner-export.json"
    path.write_text(json.dumps(worker_export), encoding="utf-8")
    return path


def assert_matches_export(directory, payload):
    for name, filename in DATABASES.items():
        source = payload["databases"][name]
        with closing(sqlite3.connect(directory / filename)) as database:
            actual_tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert actual_tables == set(source["tables"])
            for table, content in source["tables"].items():
                rows = database.execute(f"SELECT * FROM {quote(table)}").fetchall()
                expected = [tuple(decode_value(row[column]) for column in content["columns"])
                            for row in content["rows"]]
                assert rows == expected, (name, table)
                if table != "sqlite_sequence":
                    assert database.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0] == content["sql"]
            actual_schema = {(kind, table, obj, sql) for kind, table, obj, sql in database.execute(
                "SELECT type,tbl_name,name,sql FROM sqlite_master WHERE type != 'table' AND sql IS NOT NULL")}
            assert actual_schema == {(item["type"], item["tbl_name"], item["name"], item["sql"]) for item in source["schema"]}


def test_export_import_round_trip_all_worker_tables(export_path, worker_export, tmp_path):
    directory = tmp_path / "docker-data"
    counts = import_export(export_path, directory)
    assert set(counts) == {"room", "hub"}
    assert_matches_export(directory, worker_export)
    output = io.StringIO()
    assert verify_export(export_path, directory, output=output)
    assert output.getvalue().splitlines()[-1] == "PASS"
    for name, source in worker_export["databases"].items():
        for table in source["tables"]:
            assert f"{name}.{table}:" in output.getvalue()
    for filename in DATABASES.values():
        assert (directory / filename).stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(directory / "hub.db")) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 2
        assert database.execute("SELECT version FROM agentmesh_schema WHERE id=1").fetchone()[0] == 2


def test_import_idempotent_explicit_replacement_including_no_primary_key(export_path, worker_export, tmp_path):
    payload = copy.deepcopy(worker_export)
    payload["databases"]["room"]["tables"]["without_primary_key"] = {
        "sql": "CREATE TABLE without_primary_key (value TEXT)", "columns": ["value"],
        "rows": [{"value": "duplicate"}, {"value": "duplicate"}],
    }
    export_path.write_text(json.dumps(payload))
    directory = tmp_path / "data"
    import_export(export_path, directory)
    with pytest.raises(ExportError, match="non-empty"):
        import_export(export_path, directory)
    for _ in range(2):
        import_export(export_path, directory, replace=True)
        assert_matches_export(directory, payload)
        assert verify_export(export_path, directory, output=io.StringIO())


def test_import_preserves_trigger_behavior_and_sequence_high_water(export_path, worker_export, tmp_path):
    payload = copy.deepcopy(worker_export)
    room = payload["databases"]["room"]["tables"]
    room["sequence_check"] = {
        "sql": "CREATE TABLE sequence_check (id INTEGER PRIMARY KEY AUTOINCREMENT)",
        "columns": ["id"], "rows": [{"id": 4}],
    }
    room["sqlite_sequence"]["rows"].append({"name": "sequence_check", "seq": 99})
    export_path.write_text(json.dumps(payload))
    directory = tmp_path / "data"
    import_export(export_path, directory)
    with sqlite3.connect(directory / "data.db") as database:
        assert database.execute("INSERT INTO sequence_check DEFAULT VALUES").lastrowid == 100
        table = quote('future "table')
        database.execute(f"UPDATE {table} SET note='trigger still works' WHERE id=1")
        assert database.execute(f"SELECT optional FROM {table} WHERE id=1").fetchone() == ("trigger still works",)


def test_verify_export_missing_row_fails(export_path, tmp_path):
    directory = tmp_path / "data"
    import_export(export_path, directory)
    with sqlite3.connect(directory / "data.db") as database:
        assert database.execute("DELETE FROM messages WHERE rowid=(SELECT rowid FROM messages LIMIT 1)").rowcount == 1
    output = io.StringIO()
    assert not verify_export(export_path, directory, output=output)
    assert "room.messages:" in output.getvalue()
    assert "MISMATCH" in output.getvalue()
    assert output.getvalue().splitlines()[-1] == "FAIL"
    assert "PASS" not in output.getvalue()


@pytest.mark.parametrize("change", ["missing", "extra", "renamed"])
def test_verify_export_requires_exact_table_set(export_path, tmp_path, change):
    directory = tmp_path / "data"
    import_export(export_path, directory)
    with sqlite3.connect(directory / "data.db") as database:
        if change == "missing":
            database.execute("DROP TABLE messages")
        elif change == "extra":
            database.execute("CREATE TABLE unexpected_empty (id INTEGER)")
        else:
            database.execute("ALTER TABLE messages RENAME TO renamed_messages")
    output = io.StringIO()
    assert not verify_export(export_path, directory, output=output)
    assert "PASS" not in output.getvalue()


def test_verify_export_missing_database_never_creates_it(export_path, tmp_path):
    directory = tmp_path / "data"
    import_export(export_path, directory)
    (directory / "hub.db").unlink()
    with pytest.raises(ExportError, match="missing database"):
        verify_export(export_path, directory, output=io.StringIO())
    assert not (directory / "hub.db").exists()


@pytest.mark.parametrize("target", ["data.db", "hub.db"])
def test_import_preflights_both_nonempty_databases(export_path, tmp_path, target):
    directory = tmp_path / "data"
    directory.mkdir()
    with sqlite3.connect(directory / target) as database:
        database.execute("CREATE TABLE agent_tokens (token_hash BLOB)")
        database.execute("INSERT INTO agent_tokens VALUES (?)", (b"local-only-token-hash",))
    original = (directory / target).read_bytes()
    with pytest.raises(ExportError, match="non-empty"):
        import_export(export_path, directory)
    assert (directory / target).read_bytes() == original
    other = "hub.db" if target == "data.db" else "data.db"
    assert not (directory / other).exists()


def test_import_permits_existing_empty_schemas(export_path, worker_export, tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    for filename in DATABASES.values():
        with sqlite3.connect(directory / filename) as database:
            database.execute("CREATE TABLE empty_local (id TEXT)")
    import_export(export_path, directory)
    assert_matches_export(directory, worker_export)


@pytest.mark.parametrize("malformation", ["bad_sql", "missing_columns", "invalid_blob", "missing_database", "extra_database", "duplicate_rows", "bad_index", "bad_version"])
def test_import_bad_export_never_partially_replaces(export_path, worker_export, tmp_path, malformation):
    directory = tmp_path / "data"
    import_export(export_path, directory)
    originals = {name: (directory / filename).read_bytes() for name, filename in DATABASES.items()}
    payload = copy.deepcopy(worker_export)
    hub = payload["databases"]["hub"]
    if malformation == "bad_sql":
        hub["tables"]["teams"]["sql"] = "CREATE TABLE broken ("
    elif malformation == "missing_columns":
        hub["tables"]["teams"]["rows"][0].pop("id")
    elif malformation == "invalid_blob":
        hub["tables"]["teams"]["rows"][0]["name"] = {"$blob": "invalid!"}
    elif malformation == "missing_database":
        payload["databases"].pop("hub")
    elif malformation == "extra_database":
        payload["databases"]["unexpected"] = copy.deepcopy(hub)
    elif malformation == "duplicate_rows":
        hub["tables"]["teams"]["rows"].append(copy.deepcopy(hub["tables"]["teams"]["rows"][0]))
    elif malformation == "bad_index":
        hub["schema"].append({"type": "index", "name": "missing_index", "tbl_name": "teams", "sql": "DELETE FROM teams"})
    else:
        hub["tables"]["agentmesh_schema"]["rows"][0]["version"] = "wrong"
    export_path.write_text(json.dumps(payload))
    with pytest.raises((ExportError, sqlite3.Error)):
        import_export(export_path, directory, replace=True)
    for name, filename in DATABASES.items():
        assert (directory / filename).read_bytes() == originals[name]
    assert not list(directory.glob(".agentmesh-import-*"))


def test_import_failed_install_rolls_back_both_files(export_path, worker_export, tmp_path, monkeypatch):
    from agentmesh_server.scripts import import_export as importer

    directory = tmp_path / "data"
    import_export(export_path, directory)
    originals = {filename: (directory / filename).read_bytes() for filename in DATABASES.values()}
    real_replace = importer.os.replace

    def fail_hub(source, target):
        if Path(source).name == "hub.db":
            raise OSError("simulated second-file installation failure")
        return real_replace(source, target)

    monkeypatch.setattr(importer.os, "replace", fail_hub)
    with pytest.raises(OSError, match="simulated"):
        import_export(export_path, directory, replace=True)
    for filename, original in originals.items():
        assert (directory / filename).read_bytes() == original
    assert_matches_export(directory, worker_export)


@pytest.mark.parametrize("unsafe", ["directory", "parent", "database", "wal", "shm", "journal", "hardlink"])
def test_import_refuses_unsafe_destinations(export_path, tmp_path, unsafe):
    directory = tmp_path / "data"
    directory.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if unsafe == "directory":
        directory.rmdir()
        directory.symlink_to(elsewhere, target_is_directory=True)
    elif unsafe == "parent":
        link = tmp_path / "linked-parent"
        link.symlink_to(elsewhere, target_is_directory=True)
        directory = link / "new-data"
    elif unsafe == "database":
        (directory / "data.db").symlink_to(elsewhere / "db")
    elif unsafe == "hardlink":
        (elsewhere / "db").write_bytes(b"")
        (directory / "data.db").hardlink_to(elsewhere / "db")
    else:
        suffix = {"wal": "-wal", "shm": "-shm", "journal": "-journal"}[unsafe]
        (directory / ("hub.db" + suffix)).write_bytes(b"sidecar")
    with pytest.raises(ExportError):
        import_export(export_path, directory, replace=True)
    assert not (elsewhere / "db").exists() or (elsewhere / "db").read_bytes() == b""


def test_import_preserves_sequence_after_last_autoincrement_table_was_dropped(tmp_path):
    path = tmp_path / "sequence.json"
    payload = {"format": "agentmesh-sqlite-export", "version": 1, "databases": {
        name: {"tables": {"sqlite_sequence": {"sql": "CREATE TABLE sqlite_sequence(name,seq)",
                 "columns": ["name", "seq"], "rows": []}}, "schema": []}
        for name in DATABASES}}
    path.write_text(json.dumps(payload))
    import_export(path, tmp_path / "data")
    assert_matches_export(tmp_path / "data", payload)


def test_import_restores_generated_columns_without_inserting_them(tmp_path):
    path = tmp_path / "generated.json"
    payload = {"format": "agentmesh-sqlite-export", "version": 1, "databases": {
        name: {"tables": {"generated": {
            "sql": "CREATE TABLE generated (value INTEGER, doubled INTEGER GENERATED ALWAYS AS (value * 2) STORED)",
            "columns": ["value", "doubled"], "rows": [{"value": 5, "doubled": 10}]}}, "schema": []}
        for name in DATABASES}}
    path.write_text(json.dumps(payload))
    import_export(path, tmp_path / "data")
    assert_matches_export(tmp_path / "data", payload)


def test_import_creates_views_before_instead_of_triggers(tmp_path):
    path = tmp_path / "view.json"
    payload = {"format": "agentmesh-sqlite-export", "version": 1, "databases": {
        name: {"tables": {"original": {"sql": "CREATE TABLE original (value TEXT)",
            "columns": ["value"], "rows": [{"value": "original"}]}}, "schema": [
            {"type": "trigger", "name": "view_insert", "tbl_name": "editable", "sql":
                "CREATE TRIGGER view_insert INSTEAD OF INSERT ON editable BEGIN INSERT INTO original VALUES (NEW.value); END"},
            {"type": "view", "name": "editable", "tbl_name": "editable", "sql":
                "CREATE VIEW editable AS SELECT value FROM original"}]}
        for name in DATABASES}}
    path.write_text(json.dumps(payload))
    directory = tmp_path / "data"
    import_export(path, directory)
    assert_matches_export(directory, payload)
    with sqlite3.connect(directory / "data.db") as database:
        database.execute("INSERT INTO editable VALUES ('view insert')")
        assert database.execute("SELECT value FROM original").fetchall() == [("original",), ("view insert",)]


def test_import_and_verify_cli(export_path, tmp_path):
    directory = tmp_path / "data"
    importer = [sys.executable, str(ROOT / "scripts/import_export.py"), str(export_path), "--data-dir", str(directory)]
    verifier = [sys.executable, str(ROOT / "scripts/verify_export.py"), str(export_path), "--data-dir", str(directory)]
    assert subprocess.run(importer, capture_output=True, text=True, timeout=20, check=False).returncode == 0
    second = subprocess.run(importer, capture_output=True, text=True, timeout=20, check=False)
    assert second.returncode == 1 and "--replace" in second.stderr
    assert subprocess.run(importer + ["--replace"], capture_output=True, text=True, timeout=20, check=False).returncode == 0
    verified = subprocess.run(verifier, capture_output=True, text=True, timeout=20, check=False)
    assert verified.returncode == 0 and verified.stdout.splitlines()[-1] == "PASS"
    with sqlite3.connect(directory / "data.db") as database:
        database.execute("DELETE FROM messages")
    failed = subprocess.run(verifier, capture_output=True, text=True, timeout=20, check=False)
    assert failed.returncode == 1 and "PASS" not in failed.stdout
    assert failed.stdout.splitlines()[-1] == "FAIL"


def test_export_import_hub_opens_in_vendored_runtime(export_path, tmp_path):
    directory = tmp_path / "data"
    import_export(export_path, directory)
    vendor = ROOT / "server/src/agentmesh_server/vendor/openhuddle/src/hub"
    javascript = """
      const { openDb } = await import(process.argv[1]);
      const { Hub } = await import(process.argv[2]);
      const db = openDb(process.argv[3]);
      const hub = new Hub(db, { publicUrl: 'https://migration.example.invalid' });
      const member = db.prepare('SELECT * FROM members LIMIT 1').get();
      const state = hub.state(member);
      if (!state.team || state.team.id !== member.team_id) throw Error('imported team unavailable');
      db.close();
    """
    result = subprocess.run(["node", "--input-type=module", "-e", javascript,
                             (vendor / "db.js").as_uri(), (vendor / "hub.js").as_uri(),
                             str(directory / "hub.db")], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr

