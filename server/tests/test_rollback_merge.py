"""Rollback/merge regression checks use disposable SQLite databases, never a live API."""

import base64
import hashlib
import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import rollback_merge

backup_live = rollback_merge.backup_live


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def encode(value):
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    return value


def decode(value):
    if isinstance(value, dict) and set(value) == {"$base64"}:
        return base64.b64decode(value["$base64"], validate=True)
    return value


class SQLiteClient:
    """A local export/import transport backed by SQLite's actual key semantics."""

    base_url = "https://rollback.example.invalid"

    def __init__(self, room_sql="CREATE TABLE messages(id TEXT PRIMARY KEY, body TEXT);", hub_sql=None):
        hub_sql = hub_sql or "CREATE TABLE teams(id TEXT PRIMARY KEY, name TEXT);"
        self.databases = {}
        for store, schema in (("room", room_sql), ("hub", hub_sql)):
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys=ON")
            database.executescript(schema)
            self.databases[store] = database
        self.calls = []
        self.restore_statements = {}
        self.restart_failures = 0
        self.restored = set()

    def execute(self, store, statement, values=()):
        result = self.databases[store].execute(statement, values)
        self.databases[store].commit()
        return result

    def get_json(self, path, parameters=None):
        self.calls.append(("GET", path, dict(parameters or {})))
        if path == "/admin/export/tables":
            if self.restored == {"room", "hub"} and self.restart_failures:
                self.restart_failures -= 1
                raise backup_live.BackupError("export request failed with HTTP 503")
            tables = []
            for store, database in self.databases.items():
                for name, sql in database.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
                ):
                    tables.append({"store": store, "name": name, "sql": sql,
                                   "count": database.execute("SELECT COUNT(*) FROM " + quote(name)).fetchone()[0]})
            return {"tables": tables}
        assert path == "/admin/export/page"
        store, table = parameters["store"], parameters["table"]
        after = parameters.get("after")
        clause, values = ("", []) if after is None else (" WHERE rowid > ?", [int(after)])
        result = self.databases[store].execute(
            "SELECT rowid AS __fixture_rowid, * FROM " + quote(table) + clause + " ORDER BY rowid LIMIT ?",
            [*values, parameters["limit"]],
        )
        rows = [{"rowid": str(row["__fixture_rowid"]),
                 "data": {key: encode(value) for key, value in dict(row).items() if key != "__fixture_rowid"}}
                for row in result]
        return {"store": store, "table": table, "rows": rows,
                "next_after": rows[-1]["rowid"] if rows else None,
                "done": not rows}

    def post_json(self, path, body=None, parameters=None):
        parameters = dict(parameters or {})
        self.calls.append(("POST", path, parameters, body))
        store = parameters["store"]
        if path == "/admin/restore":
            requested = int(parameters["time"])
            effective = max(1_700_000_000_000, requested)
            dry_run = str(parameters["dry_run"]) == "1"
            if not dry_run:
                for statement in self.restore_statements.get(store, []):
                    self.execute(store, statement)
                self.restored.add(store)
            return {"store": store, "time": effective, "requested_time": requested,
                    "bookmark": "fixture-" + store, "dry_run": dry_run}
        assert path == "/admin/import"
        table = parameters["table"]
        database = self.databases[store]
        schema = list(database.execute("PRAGMA table_info(" + quote(table) + ")"))
        primary = [row["name"] for row in sorted(schema, key=lambda row: row["pk"]) if row["pk"]]
        if table == "sqlite_sequence":
            primary = ["name"]
        inserted = 0
        for envelope in body["rows"]:
            data = {name: decode(value) for name, value in envelope["data"].items()}
            use_primary = primary and all(data[name] is not None for name in primary)
            if use_primary:
                condition = " AND ".join(quote(name) + " IS ?" for name in primary)
                key = [data[name] for name in primary]
            else:
                condition, key = "rowid = ?", [int(envelope["rowid"])]
            if database.execute("SELECT 1 FROM " + quote(table) + " WHERE " + condition, key).fetchone():
                continue
            names, values = list(data), list(data.values())
            if not use_primary:
                names.insert(0, "rowid")
                values.insert(0, int(envelope["rowid"]))
            placeholders = ",".join("?" for _ in names)
            database.execute("INSERT OR IGNORE INTO " + quote(table) + " (" + ",".join(map(quote, names))
                             + ") VALUES (" + placeholders + ")", values)
            inserted += database.execute("SELECT changes()").fetchone()[0]
        database.commit()
        return {"inserted": inserted, "skipped": len(body["rows"]) - inserted}


def make_backup(tmp_path, client):
    return backup_live.backup(client, tmp_path / "backups", page_limit=2)


def snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def imports(client):
    return [call for call in client.calls if call[0:2] == ("POST", "/admin/import")]


def restores(client):
    return [call for call in client.calls if call[0:2] == ("POST", "/admin/restore")]


def test_merge_missing_primary_keys_keeps_existing_values_and_is_idempotent(tmp_path):
    source = SQLiteClient()
    source.execute("room", "INSERT INTO messages(rowid,id,body) VALUES(1,'old','backup old')")
    source.execute("room", "INSERT INTO messages(rowid,id,body) VALUES(2,'missing','backup missing')")
    source.execute("hub", "INSERT INTO teams VALUES('team','original team')")
    directory = make_backup(tmp_path, source)
    before = snapshot(directory)
    tables = rollback_merge.load_backup(directory)
    live = SQLiteClient()
    # A key's rowid may change after restore: merge by its declared PK, not rowid.
    live.execute("room", "INSERT INTO messages(rowid,id,body) VALUES(200,'old','live wins')")
    live.execute("room", "INSERT INTO messages(rowid,id,body) VALUES(2,'extra','keep extra')")
    result = rollback_merge.merge_backup(live, tables, page_limit=1)
    assert result["inserted"] == 2
    assert dict(live.execute("room", "SELECT id,body FROM messages")) == {
        "old": "live wins", "missing": "backup missing", "extra": "keep extra",
    }
    assert rollback_merge.verify_backup(live, tables, page_limit=1) is True
    calls_before = len(imports(live))
    assert rollback_merge.merge_backup(live, tables, page_limit=1)["inserted"] == 0
    assert len(imports(live)) == calls_before
    assert snapshot(directory) == before  # No writes or cleanup of the backup.


def test_merge_and_verifier_use_composite_primary_keys(tmp_path, capsys):
    schema = "CREATE TABLE messages(team TEXT, id TEXT, body TEXT, PRIMARY KEY(team,id));"
    source = SQLiteClient(room_sql=schema)
    source.execute("room", "INSERT INTO messages VALUES('a','one','backup a')")
    source.execute("room", "INSERT INTO messages VALUES('b','one','backup b')")
    tables = rollback_merge.load_backup(make_backup(tmp_path, source))
    live = SQLiteClient(room_sql=schema)
    live.execute("room", "INSERT INTO messages(rowid,team,id,body) VALUES(90,'a','one','unchanged')")
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 1
    assert tuple(live.execute("room", "SELECT team,id,body FROM messages WHERE team='a'").fetchone()) == (
        "a", "one", "unchanged",
    )
    assert rollback_merge.verify_backup(live, tables) is True
    assert "PASS" in capsys.readouterr().out


def test_verifier_fails_on_missing_backup_key_even_when_live_count_is_high_enough(tmp_path, capsys):
    source = SQLiteClient()
    source.execute("room", "INSERT INTO messages VALUES('required','must exist')")
    tables = rollback_merge.load_backup(make_backup(tmp_path, source))
    live = SQLiteClient()
    live.execute("room", "INSERT INTO messages VALUES('unrelated','does not count')")
    live.execute("room", "INSERT INTO messages VALUES('another','also unrelated')")
    assert rollback_merge.verify_backup(live, tables) is False
    assert "FAIL" in capsys.readouterr().out


def test_keyless_rows_keep_signed_int64_rowids_and_blob_values(tmp_path):
    schema = "CREATE TABLE messages(body TEXT, payload BLOB);"
    source = SQLiteClient(room_sql=schema)
    for rowid, body in ((-(2**63), "negative"), (0, "zero"), (2**63 - 1, "maximum")):
        source.execute("room", "INSERT INTO messages(rowid,body,payload) VALUES(?,?,?)",
                       (rowid, body, b"\x00\xff\x80\x01"))
    tables = rollback_merge.load_backup(make_backup(tmp_path, source))
    live = SQLiteClient(room_sql=schema)
    live.execute("room", "INSERT INTO messages(rowid,body,payload) VALUES(0,'existing',?)", (b"original",))
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 2
    assert [tuple(row) for row in live.execute("room", "SELECT rowid,body,payload FROM messages ORDER BY rowid")] == [
        (-(2**63), "negative", b"\x00\xff\x80\x01"), (0, "existing", b"original"),
        (2**63 - 1, "maximum", b"\x00\xff\x80\x01"),
    ]
    assert rollback_merge.verify_backup(live, tables) is True
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 0


def test_sqlite_sequence_is_keyed_by_name_not_rowid_and_never_overwrites(tmp_path):
    schema = "CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT);"
    source = SQLiteClient(room_sql=schema)
    source.execute("room", "INSERT INTO sqlite_sequence(rowid,name,seq) VALUES(1,'existing',10)")
    source.execute("room", "INSERT INTO sqlite_sequence(rowid,name,seq) VALUES(2,'missing',20)")
    tables = rollback_merge.load_backup(make_backup(tmp_path, source))
    live = SQLiteClient(room_sql=schema)
    live.execute("room", "INSERT INTO sqlite_sequence(rowid,name,seq) VALUES(90,'existing',500)")
    live.execute("room", "INSERT INTO sqlite_sequence(rowid,name,seq) VALUES(2,'unrelated',900)")
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 1
    assert dict(live.execute("room", "SELECT name,seq FROM sqlite_sequence")) == {
        "existing": 500, "missing": 20, "unrelated": 900,
    }
    assert rollback_merge.verify_backup(live, tables) is True
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 0
    assert live.execute("room", "SELECT COUNT(*) FROM sqlite_sequence WHERE name='existing'").fetchone()[0] == 1


def test_merge_orders_parent_tables_before_children_with_sqlite_foreign_keys(tmp_path):
    schema = ("CREATE TABLE a_children(id TEXT PRIMARY KEY, parent_id TEXT REFERENCES z_parents(id));"
              "CREATE TABLE z_parents(id TEXT PRIMARY KEY, body TEXT);")
    source = SQLiteClient(room_sql=schema)
    source.execute("room", "INSERT INTO z_parents VALUES('parent','original')")
    source.execute("room", "INSERT INTO a_children VALUES('child','parent')")
    tables = rollback_merge.load_backup(make_backup(tmp_path, source))
    live = SQLiteClient(room_sql=schema)
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 2
    assert [call[2]["table"] for call in imports(live)] == ["z_parents", "a_children"]
    assert rollback_merge.verify_backup(live, tables) is True


@pytest.mark.parametrize("payload", ["small", "\u2603" * 1000])
def test_import_batches_obey_row_and_encoded_byte_limits(tmp_path, payload):
    source = SQLiteClient()
    source.databases["room"].executemany("INSERT INTO messages VALUES(?,?)", [
        (str(index), payload) for index in range(1100)
    ])
    source.databases["room"].commit()
    tables = rollback_merge.load_backup(make_backup(tmp_path, source))
    live = SQLiteClient()
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 1100
    batches = imports(live)
    assert len(batches) >= 2
    assert sum(len(call[3]["rows"]) for call in batches) == 1100
    for call in batches:
        assert 0 < len(call[3]["rows"]) <= 1000
        assert len(rollback_merge._encode(call[3])) <= 2 * 1024 * 1024


@pytest.mark.parametrize("corruption", [
    "incomplete", "hash", "count", "expected_count", "missing_file", "unsafe_filename",
    "duplicate_table", "duplicate_file", "format", "bad_sql", "unknown_column",
])
def test_corrupt_backup_is_rejected_before_any_remote_mutation(tmp_path, corruption):
    source = SQLiteClient()
    source.execute("room", "INSERT INTO messages VALUES('saved','original')")
    directory = make_backup(tmp_path, source)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(table for table in manifest["tables"] if table["store"] == "room")
    if corruption == "incomplete":
        manifest["complete"] = False
    elif corruption == "hash":
        entry["sha256"] = "0" * 64
    elif corruption in ("count", "expected_count"):
        entry[corruption] += 1
    elif corruption == "missing_file":
        entry["file"] = "missing.json"
    elif corruption == "unsafe_filename":
        entry["file"] = "../outside.json"
    elif corruption == "duplicate_table":
        manifest["tables"].append(dict(entry))
    elif corruption == "duplicate_file":
        manifest["tables"][1]["file"] = manifest["tables"][0]["file"]
    elif corruption == "format":
        manifest["format_version"] = 9000
    elif corruption == "bad_sql":
        entry["sql"] = "DROP TABLE messages"
    elif corruption == "unknown_column":
        row_path = directory / entry["file"]
        rows = json.loads(row_path.read_text())
        rows[0]["data"]["unknown"] = "not a schema column"
        row_path.write_text(json.dumps(rows))
        entry["sha256"] = hashlib.sha256(row_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    live = SQLiteClient()
    with pytest.raises((backup_live.BackupError, OSError)):
        rollback_merge.rollback_merge(live, directory, wait_timeout=0.1)
    assert not restores(live)
    assert not imports(live)


def test_restore_runs_both_dry_runs_before_real_restore_and_waits_before_import(tmp_path, monkeypatch):
    source = SQLiteClient()
    source.execute("room", "INSERT INTO messages VALUES('missing','preserved')")
    source.execute("hub", "INSERT INTO teams VALUES('team','preserved team')")
    directory = make_backup(tmp_path, source)
    before = snapshot(directory)
    live = SQLiteClient()
    live.restart_failures = 1
    monkeypatch.setattr(rollback_merge.time, "sleep", lambda seconds: None)
    assert rollback_merge.rollback_merge(live, directory, wait_timeout=1) is True
    calls = restores(live)
    assert [(call[2]["store"], str(call[2]["dry_run"])) for call in calls] == [
        ("room", "1"), ("hub", "1"), ("room", "0"), ("hub", "0"),
    ]
    assert all(int(call[2]["time"]) == 0 for call in calls[:2])
    assert all(int(call[2]["time"]) == 1_700_000_000_000 for call in calls[2:])
    last_restore = max(index for index, call in enumerate(live.calls) if call[0:2] == ("POST", "/admin/restore"))
    first_import = min(index for index, call in enumerate(live.calls) if call[0:2] == ("POST", "/admin/import"))
    assert any(call[0:2] == ("GET", "/admin/export/tables") for call in live.calls[last_restore + 1:first_import])
    assert live.restart_failures == 0
    assert snapshot(directory) == before


def test_failed_second_dry_run_never_schedules_either_restore(tmp_path):
    directory = make_backup(tmp_path, SQLiteClient())

    class DryFailure(SQLiteClient):
        def post_json(self, path, body=None, parameters=None):
            if path == "/admin/restore" and parameters["store"] == "hub":
                raise backup_live.BackupError("dry run failed")
            return super().post_json(path, body, parameters)

    live = DryFailure()
    with pytest.raises(backup_live.BackupError):
        rollback_merge.rollback_merge(live, directory)
    assert all(str(call[2]["dry_run"]) == "1" for call in restores(live))
    assert not imports(live)


def test_failed_real_restore_stops_without_importing(tmp_path):
    directory = make_backup(tmp_path, SQLiteClient())

    class RestoreFailure(SQLiteClient):
        def post_json(self, path, body=None, parameters=None):
            if path == "/admin/restore" and str(parameters["dry_run"]) == "0":
                raise backup_live.BackupError("restore failed")
            return super().post_json(path, body, parameters)

    live = RestoreFailure()
    with pytest.raises(backup_live.BackupError):
        rollback_merge.rollback_merge(live, directory)
    assert not imports(live)


def test_schema_mismatch_is_rejected_before_restore(tmp_path):
    directory = make_backup(tmp_path, SQLiteClient())
    live = SQLiteClient(room_sql="CREATE TABLE messages(id TEXT PRIMARY KEY, renamed_body TEXT);")
    with pytest.raises(backup_live.BackupError):
        rollback_merge.rollback_merge(live, directory)
    assert not restores(live)
    assert not imports(live)


def test_backup_source_must_match_target_before_restore(tmp_path):
    directory = make_backup(tmp_path, SQLiteClient())
    live = SQLiteClient()
    live.base_url = "https://different.example.invalid"
    with pytest.raises(backup_live.BackupError):
        rollback_merge.rollback_merge(live, directory)
    assert not restores(live)
    assert not imports(live)


def test_backup_root_chooses_newest_complete_manifest_not_directory_name(tmp_path):
    source = SQLiteClient()
    older = make_backup(tmp_path, source)
    source.execute("room", "INSERT INTO messages VALUES('newest','keep')")
    newest = make_backup(tmp_path, source)
    for directory, created in ((older, "2020-01-01T00:00:00Z"), (newest, "2025-01-01T00:00:00Z")):
        path = directory / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["created_at"] = created
        path.write_text(json.dumps(manifest))
    incomplete = tmp_path / "backups" / "zzzz-incomplete"
    incomplete.mkdir()
    (incomplete / "manifest.json").write_text(json.dumps({"complete": False, "created_at": "2030-01-01T00:00:00Z"}))
    tables = rollback_merge.load_backup(tmp_path / "backups")
    room = next(table for table in tables if table["store"] == "room" and table["table"] == "messages")
    assert room["count"] == 1
    assert room["rows"][0]["data"]["id"] == "newest"


def test_nullable_declared_primary_keys_use_original_rowids_without_duplicates(tmp_path):
    source = SQLiteClient()
    source.execute("room", "INSERT INTO messages(rowid,id,body) VALUES(-1,NULL,'backup existing')")
    source.execute("room", "INSERT INTO messages(rowid,id,body) VALUES(2,NULL,'backup missing')")
    tables = rollback_merge.load_backup(make_backup(tmp_path, source))
    live = SQLiteClient()
    live.execute("room", "INSERT INTO messages(rowid,id,body) VALUES(-1,NULL,'live wins')")
    live.execute("room", "INSERT INTO messages(rowid,id,body) VALUES(99,NULL,'unrelated')")
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 1
    assert [tuple(row) for row in live.execute("room", "SELECT rowid,id,body FROM messages ORDER BY rowid")] == [
        (-1, None, "live wins"), (2, None, "backup missing"), (99, None, "unrelated"),
    ]
    assert rollback_merge.verify_backup(live, tables) is True
    assert rollback_merge.merge_backup(live, tables)["inserted"] == 0


def test_cli_dry_run_validates_and_resolves_times_without_any_mutation(tmp_path):
    directory = make_backup(tmp_path, SQLiteClient())
    before = snapshot(directory)
    live = SQLiteClient()
    live.execute("room", "INSERT INTO messages VALUES('live','unchanged')")
    assert rollback_merge.rollback_merge(live, directory, dry_run=True) is True
    assert len(restores(live)) == 2
    assert all(str(call[2]["dry_run"]) == "1" for call in restores(live))
    assert not imports(live)
    assert live.execute("room", "SELECT body FROM messages WHERE id='live'").fetchone()[0] == "unchanged"
    assert snapshot(directory) == before


def test_restart_timeout_never_imports_or_retries_restore(tmp_path, monkeypatch):
    directory = make_backup(tmp_path, SQLiteClient())

    class NeverReady(SQLiteClient):
        def get_json(self, path, parameters=None):
            if self.restored:
                raise backup_live.BackupError("export request failed with HTTP 503")
            return super().get_json(path, parameters)

    live = NeverReady()
    monkeypatch.setattr(rollback_merge.time, "sleep", lambda seconds: None)
    with pytest.raises(backup_live.BackupError, match="timed out"):
        rollback_merge.rollback_merge(live, directory, wait_timeout=0.001)
    real_restores = [call for call in restores(live) if str(call[2]["dry_run"]) == "0"]
    assert len(real_restores) == len({call[2]["store"] for call in real_restores})
    assert not imports(live)


def test_verifier_reports_missing_schema_as_failure(tmp_path, capsys):
    tables = rollback_merge.load_backup(make_backup(tmp_path, SQLiteClient()))
    live = SQLiteClient()
    live.execute("hub", "DROP TABLE teams")
    assert rollback_merge.verify_backup(live, tables) is False
    assert "FAIL" in capsys.readouterr().out


def test_oversized_single_row_is_rejected_before_any_restore(tmp_path):
    source = SQLiteClient()
    # Existing export supports 2 MiB pages, but a response-size fixture can
    # still represent a hand-edited manifest: never discover this after rollback.
    source.execute("room", "INSERT INTO messages VALUES('large',?)", ("x" * (2 * 1024 * 1024),))
    directory = make_backup(tmp_path, source)
    live = SQLiteClient()
    with pytest.raises(backup_live.BackupError, match="byte limit"):
        rollback_merge.rollback_merge(live, directory)
    assert not restores(live)
    assert not imports(live)


@contextmanager
def local_http(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_restore_http_transport_preserves_body_query_and_private_headers():
    calls = []
    token = "fixture-owner-token-never-production-123456789"
    body = {"rows": [{"rowid": "9223372036854775807", "data": {
        "id": "example", "body": "snowman \u2603", "payload": {"$base64": "AP+A"},
    }}]}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.command, self.path, dict(self.headers), payload))
            response = b'{"inserted":1,"skipped":0}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    parameters = {"store": "room", "table": 'quoted / "table\u2603'}
    with local_http(Handler) as origin:
        url = backup_live.validate_url(origin, allow_local_http=True)
        client = rollback_merge.RestoreClient(url, token, "fixture-access-id", "fixture-access-secret", delay=0)
        assert client.post_json("/admin/import", body, parameters) == {"inserted": 1, "skipped": 0}
    method, path, headers, payload = calls[0]
    assert len(calls) == 1
    assert method == "POST"
    assert urlsplit(path).path == "/admin/import"
    assert parse_qs(urlsplit(path).query) == {key: [value] for key, value in parameters.items()}
    assert headers["Authorization"] == "Bearer " + token
    assert headers["User-Agent"] == "agentmesh-backup/1.0"
    assert headers["Cf-Access-Client-Id"] == "fixture-access-id"
    assert headers["Cf-Access-Client-Secret"] == "fixture-access-secret"
    assert headers["Content-Type"] == "application/json"
    assert payload == rollback_merge._encode(body)
    assert "\u2603".encode("utf-8") in payload
    assert json.loads(payload) == body


@pytest.mark.parametrize("status", [302, 307, 308])
def test_restore_http_redirect_does_not_replay_post_or_leak_credentials(status):
    origin_calls, target_calls = [], []
    token = "fixture-owner-token-never-production-123456789"

    class Target(BaseHTTPRequestHandler):
        def do_POST(self):
            target_calls.append((self.command, self.path, dict(self.headers)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST

        def log_message(self, *args):
            pass

    with local_http(Target) as target:
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                origin_calls.append((self.command, self.path))
                self.send_response(status)
                self.send_header("Location", target + "/must-not-receive-owner-token")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *args):
                pass

        with local_http(Redirect) as origin:
            client = rollback_merge.RestoreClient(origin, token, "fixture-access-id", "fixture-access-secret", delay=0)
            with pytest.raises(backup_live.BackupError, match=f"HTTP {status}") as error:
                client.post_json("/admin/restore", parameters={"store": "room", "time": 0, "dry_run": "0"})
    assert origin_calls == [("POST", "/admin/restore?store=room&time=0&dry_run=0")]
    assert target_calls == []
    assert token not in str(error.value)
    assert "fixture-access-secret" not in str(error.value)


@pytest.mark.parametrize("verified, expected_exit", [(False, 1), (True, 0)])
def test_main_maps_verification_to_exit_code_without_reading_tokens_or_network(
    tmp_path, monkeypatch, capsys, verified, expected_exit,
):
    token = "fixture-owner-token-never-production-123456789"
    settings_calls, client_calls, merge_calls = [], [], []
    client = object()
    tokens_file, backup_dir = tmp_path / "never-read.conf", tmp_path / "never-read-backups"

    def fake_settings(path):
        settings_calls.append(path)
        return {"ADMIN_EXPORT_TOKEN": token, "AGENTMESH_URL": "https://rollback.example.invalid"}

    def fake_client(*args):
        client_calls.append(args)
        return client

    def fake_rollback(*args):
        merge_calls.append(args)
        return verified

    monkeypatch.setattr(backup_live, "load_settings", fake_settings)
    monkeypatch.setattr(rollback_merge, "RestoreClient", fake_client)
    monkeypatch.setattr(rollback_merge, "rollback_merge", fake_rollback)
    assert rollback_merge.main([
        "--tokens-file", str(tokens_file), "--backup-dir", str(backup_dir),
        "--page-limit", "25", "--wait-timeout", "15", "--request-delay", "0",
    ]) == expected_exit
    assert settings_calls == [tokens_file]
    assert client_calls == [("https://rollback.example.invalid", token, "", "", 60, 0)]
    assert merge_calls == [(client, backup_dir, 25, 15, False)]
    assert not tokens_file.exists()
    assert not backup_dir.exists()
    output = capsys.readouterr()
    assert token not in output.out + output.err
