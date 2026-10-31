"""Complete backup -> two local SQLite files; no live API or private config."""

import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import import_backup_local
from test_rollback_merge import SQLiteClient, make_backup, snapshot


@pytest.fixture(autouse=True)
def restore_process_umask():
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


def target_rows(directory, file, table):
    with closing(sqlite3.connect(directory / file)) as db:
        return db.execute("SELECT * FROM " + import_backup_local.quote(table) + " ORDER BY rowid").fetchall()


def test_local_import_round_trip_counts_rerun_refusal_and_insert_only_merge(tmp_path, capsys):
    source = SQLiteClient()
    source.execute("room", "INSERT INTO messages VALUES('first','preserved snowman ☃')")
    source.execute("room", "INSERT INTO messages VALUES('second','second body')")
    source.execute("hub", "INSERT INTO teams VALUES('team','original team')")
    backup = make_backup(tmp_path, source)
    original_backup = snapshot(backup)
    output = tmp_path / "local"

    results = import_backup_local.import_backup(backup, output)
    assert sum(result["inserted"] for result in results) == 3
    assert all(result["expected"] == result["matched"] == result["total"] for result in results)
    assert target_rows(output, "data.db", "messages") == [("first", "preserved snowman ☃"), ("second", "second body")]
    assert target_rows(output, "hub.db", "teams") == [("team", "original team")]
    assert "matched=2 expected=2 inserted=2" in capsys.readouterr().out

    before = snapshot(output)
    with pytest.raises(import_backup_local.BackupError, match="non-empty.*--replace"):
        import_backup_local.import_backup(backup, output)
    assert snapshot(output) == before
    results = import_backup_local.import_backup(backup, output, replace=True)
    assert sum(result["inserted"] for result in results) == 0
    assert sum(result["skipped"] for result in results) == 3

    # A permitted merge must never replace existing values or delete extra keys.
    with closing(sqlite3.connect(output / "data.db")) as db:
        db.execute("UPDATE messages SET body='local wins' WHERE id='first'")
        db.execute("INSERT INTO messages VALUES('extra','never delete')")
        db.commit()
    results = import_backup_local.import_backup(backup, output, replace=True)
    assert sum(result["inserted"] for result in results) == 0
    assert target_rows(output, "data.db", "messages") == [
        ("first", "local wins"), ("second", "second body"), ("extra", "never delete"),
    ]
    assert snapshot(backup) == original_backup
    assert (output / "data.db").stat().st_mode & 0o777 == 0o600
    assert (output / "hub.db").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["incomplete", "count", "hash", "missing-file"])
def test_invalid_backups_are_rejected_before_creating_destination(tmp_path, damage):
    source = SQLiteClient()
    source.execute("room", "INSERT INTO messages VALUES('id','original')")
    backup = make_backup(tmp_path, source)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "incomplete":
        manifest["complete"] = False
    elif damage == "count":
        manifest["tables"][0]["count"] += 1
        manifest["tables"][0]["expected_count"] += 1
    elif damage == "hash":
        manifest["tables"][0]["sha256"] = "0" * 64
    else:
        manifest["tables"][0]["file"] = "missing.json"
    manifest_path.write_text(json.dumps(manifest))
    output = tmp_path / "local"
    with pytest.raises(import_backup_local.BackupError):
        import_backup_local.import_backup(backup, output)
    assert not output.exists()


def test_keyless_signed_rowids_blobs_and_composite_primary_keys_round_trip(tmp_path):
    source = SQLiteClient(room_sql="CREATE TABLE messages(body TEXT, payload BLOB)",
                          hub_sql="CREATE TABLE teams(team TEXT, id TEXT, value TEXT, PRIMARY KEY(team,id))")
    for rowid in (-(2 ** 63), 0, 2 ** 63 - 1):
        source.execute("room", "INSERT INTO messages(rowid,body,payload) VALUES(?,?,?)", (rowid, str(rowid), b"\x00\xff"))
    source.execute("hub", "INSERT INTO teams VALUES('a','one','first')")
    source.execute("hub", "INSERT INTO teams VALUES('b','one','second')")
    backup = make_backup(tmp_path, source)
    output = tmp_path / "local"
    import_backup_local.import_backup(backup, output)
    with closing(sqlite3.connect(output / "data.db")) as db:
        assert db.execute("SELECT rowid,body,payload FROM messages ORDER BY rowid").fetchall() == [
            (rowid, str(rowid), b"\x00\xff") for rowid in (-(2 ** 63), 0, 2 ** 63 - 1)
        ]
    assert target_rows(output, "hub.db", "teams") == [("a", "one", "first"), ("b", "one", "second")]
    assert sum(result["inserted"] for result in import_backup_local.import_backup(backup, output, replace=True)) == 0


def test_autoincrement_sequence_high_water_is_preserved(tmp_path):
    source = SQLiteClient(room_sql="CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT)")
    source.execute("room", "INSERT INTO messages VALUES(3,'saved')")
    source.execute("room", "UPDATE sqlite_sequence SET seq=100 WHERE name='messages'")
    backup = make_backup(tmp_path, source)
    output = tmp_path / "local"
    results = import_backup_local.import_backup(backup, output)
    assert all(result["total"] == result["expected"] for result in results)
    assert target_rows(output, "data.db", "sqlite_sequence") == [("messages", 100)]
    assert sum(result["inserted"] for result in import_backup_local.import_backup(backup, output, replace=True)) == 0
    with closing(sqlite3.connect(output / "data.db")) as db:
        assert db.execute("INSERT INTO messages(body) VALUES('new')").lastrowid == 101


def test_unique_conflict_rolls_back_both_stores_and_never_deletes(tmp_path):
    source = SQLiteClient(hub_sql="CREATE TABLE teams(id TEXT PRIMARY KEY, name TEXT UNIQUE)")
    source.execute("room", "INSERT INTO messages VALUES('missing','must roll back')")
    source.execute("hub", "INSERT INTO teams VALUES('backup-key','same unique value')")
    backup = make_backup(tmp_path, source)
    output = tmp_path / "local"
    output.mkdir()
    with closing(sqlite3.connect(output / "hub.db")) as db:
        db.execute("CREATE TABLE teams(id TEXT PRIMARY KEY, name TEXT UNIQUE)")
        db.execute("INSERT INTO teams VALUES('local-key','same unique value')")
        db.commit()
    with pytest.raises(import_backup_local.BackupError, match="keys/counts"):
        import_backup_local.import_backup(backup, output, replace=True)
    assert target_rows(output, "hub.db", "teams") == [("local-key", "same unique value")]
    with closing(sqlite3.connect(output / "data.db")) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_insert_trigger_cannot_delete_existing_data(tmp_path):
    source = SQLiteClient()
    source.execute("room", "INSERT INTO messages VALUES('new','saved')")
    backup = make_backup(tmp_path, source)
    output = tmp_path / "local"
    output.mkdir()
    with closing(sqlite3.connect(output / "data.db")) as db:
        db.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO messages VALUES('old','keep')")
        db.execute("CREATE TRIGGER destructive AFTER INSERT ON messages BEGIN DELETE FROM messages WHERE id='old'; END")
        db.commit()
    with pytest.raises(sqlite3.DatabaseError):
        import_backup_local.import_backup(backup, output, replace=True)
    assert target_rows(output, "data.db", "messages") == [("old", "keep")]


@pytest.mark.parametrize("drift", ["missing-unique", "changed-unique", "missing-check", "changed-check",
                                   "fk-action", "fk-target-column", "fk-local-column"])
def test_constraint_schema_drift_rolls_back_both_stores(tmp_path, drift):
    parents = "CREATE TABLE parents(id TEXT PRIMARY KEY, alternate TEXT UNIQUE);"
    teams = ("CREATE TABLE teams(id TEXT PRIMARY KEY, parent TEXT, alternate TEXT, "
             "name TEXT UNIQUE CHECK(name != 'blocked'), "
             "FOREIGN KEY(parent) REFERENCES parents(id) ON DELETE CASCADE)")
    replacements = {
        "missing-unique": ("name TEXT UNIQUE", "name TEXT"),
        "changed-unique": ("name TEXT UNIQUE", "name TEXT"),
        "missing-check": (" CHECK(name != 'blocked')", ""),
        "changed-check": ("'blocked'", "'different'"),
        "fk-action": ("ON DELETE CASCADE", "ON DELETE RESTRICT"),
        "fk-target-column": ("REFERENCES parents(id)", "REFERENCES parents(alternate)"),
        "fk-local-column": ("FOREIGN KEY(parent)", "FOREIGN KEY(alternate)"),
    }
    existing_schema = teams.replace(*replacements[drift])
    if drift == "changed-unique":
        existing_schema = existing_schema[:-1] + ", UNIQUE(name,parent))"
    source = SQLiteClient(room_sql="CREATE TABLE messages(id TEXT PRIMARY KEY, body TEXT);"
                          "CREATE TABLE imported_aux(id TEXT PRIMARY KEY);", hub_sql=parents + teams)
    source.execute("room", "INSERT INTO messages VALUES('new','backup')")
    source.execute("hub", "INSERT INTO parents VALUES('parent','parent')")
    source.execute("hub", "INSERT INTO teams VALUES('new','parent','parent','backup name')")
    backup = make_backup(tmp_path, source)
    output = tmp_path / "local"
    output.mkdir()
    with closing(sqlite3.connect(output / "data.db")) as db:
        db.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO messages VALUES('old','keep legacy')")
        db.commit()
    with closing(sqlite3.connect(output / "hub.db")) as db:
        db.executescript(parents + existing_schema)
        db.execute("INSERT INTO parents VALUES('parent','parent')")
        db.execute("INSERT INTO teams VALUES('old','parent','parent','keep team')")
        db.commit()
    original_files = snapshot(output)
    with pytest.raises(import_backup_local.BackupError, match="schemas do not match"):
        import_backup_local.import_backup(backup, output, replace=True)
    assert snapshot(output) == original_files
    assert target_rows(output, "data.db", "messages") == [("old", "keep legacy")]
    assert target_rows(output, "hub.db", "teams") == [("old", "parent", "parent", "keep team")]
    with closing(sqlite3.connect(output / "data.db")) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='imported_aux'").fetchone() is None


def test_schema_comparison_ignores_formatting_but_preserves_quoted_literal_content(tmp_path):
    source = SQLiteClient(hub_sql="CREATE TABLE teams(id TEXT PRIMARY KEY, name TEXT UNIQUE "
                          "CHECK(name != 'Don''t  change--/*Case*/'))")
    source.execute("hub", "INSERT INTO teams VALUES('saved','backup name')")
    backup = make_backup(tmp_path, source)
    output = tmp_path / "local"
    output.mkdir()
    with closing(sqlite3.connect(output / "hub.db")) as db:
        db.execute("create table teams ( id text /* insignificant */ primary key,\n"
                   " name text unique -- insignificant line comment\n"
                   " check ( name != 'Don''t  change--/*Case*/' ) )")
    results = import_backup_local.import_backup(backup, output)
    assert sum(result["inserted"] for result in results) == 1
    assert sum(result["inserted"] for result in import_backup_local.import_backup(backup, output, replace=True)) == 0
    assert import_backup_local._normalized_sql("CHECK(name != 'two  spaces')") != (
        import_backup_local._normalized_sql("CHECK(name != 'two spaces')")
    )
    assert import_backup_local._normalized_sql("CHECK(name != 'Case')") != (
        import_backup_local._normalized_sql("CHECK(name != 'case')")
    )


def test_worker_schema_import_is_usable_by_python_and_unmodified_node_hub(tmp_path):
    # Read the actual checked-in Worker schema rather than a divergent fixture.
    worker = (ROOT / "cloudflare/src/index.ts").read_text()
    room_schema = re.search(r"ctx\.storage\.sql\.exec\(`(.*?)`\)", worker, re.DOTALL).group(1)
    coordination = (ROOT / "cloudflare/src/coordination.ts").read_text()
    room_schema += re.search(r"this\.sql\(`\s*(CREATE TABLE.*?)`\)", coordination, re.DOTALL).group(1)
    source = SQLiteClient(room_sql=room_schema)
    for name, kind in (("owner", "TEXT"), ("file_areas", "TEXT"), ("depends_on", "TEXT"), ("contract", "INTEGER DEFAULT 0")):
        source.execute("room", f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
    hub_worker = (ROOT / "cloudflare/src/openhuddle/src/hub/db-do.js").read_text()
    hub_schema = re.search(r"const SCHEMA = `(.*?)`;", hub_worker, re.DOTALL).group(1)
    hub_schema += re.search(r"const HUDDLE_TABLES = `(.*?)`;", hub_worker, re.DOTALL).group(1)
    hub_schema += "ALTER TABLE teams ADD COLUMN next_huddle INTEGER NOT NULL DEFAULT 1;"
    hub_schema += "CREATE TABLE agentmesh_schema(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);"
    hub_schema += "INSERT INTO agentmesh_schema VALUES(1,2);"
    # Replace only this in-memory fixture connection, never any on-disk DB.
    source.databases["hub"].close()
    source.databases["hub"] = sqlite3.connect(":memory:")
    source.databases["hub"].row_factory = sqlite3.Row
    source.databases["hub"].executescript(hub_schema)
    source.execute("hub", "INSERT INTO teams(id,name,settings,invite_secret,invite_expires_at,prompt,created_at) "
                   "VALUES('imported','Imported team','{}','offline-fixture',0,'saved prompt',1)")
    backup = make_backup(tmp_path, source)
    output = tmp_path / "local"
    results = import_backup_local.import_backup(backup, output)
    assert all(result["matched"] == result["expected"] == result["total"] for result in results)
    with closing(sqlite3.connect(output / "hub.db")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")} >= {
            "members_team", "events_team",
        }
    from agentmesh_server.config import init_config
    from agentmesh_server.state import State
    config = init_config(output)
    state = State(output, config)
    try:
        assert state.db.execute("SELECT status FROM master_state WHERE id=1").fetchone()[0] == "draft"
    finally:
        state.close()
    from agentmesh_server.hub import find_node
    node = find_node()
    assert node is not None, "Node.js >=22.13 is required for the local hub"
    db_module = (ROOT / "server/src/agentmesh_server/vendor/openhuddle/src/hub/db.js").as_uri()
    code = ("import {openDb} from " + json.dumps(db_module) + "; const db=openDb(process.argv[1]);"
            "console.log(db.prepare('SELECT name FROM teams WHERE id=?').get('imported').name);db.close();")
    result = subprocess.run([node, "--input-type=module", "-e", code, str(output / "hub.db")],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Imported team"


def test_import_cli_has_offline_merge_help_and_refuses_populated_db(tmp_path, capsys):
    source = SQLiteClient()
    source.execute("hub", "INSERT INTO teams VALUES('team','saved')")
    backup = make_backup(tmp_path, source)
    args = ["--backup-dir", str(backup), "--config-dir", str(tmp_path / "local")]
    assert import_backup_local.main(args) == 0
    assert import_backup_local.main(args) == 1
    assert "--replace" in capsys.readouterr().err
    assert import_backup_local.main([*args, "--replace"]) == 0
