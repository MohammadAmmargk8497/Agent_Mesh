"""Owner backups use local fixtures only; no production endpoint or credentials."""

import hashlib
import importlib.util
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("backup_live", ROOT / "scripts" / "backup_live.py")
backup_live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_live)


@pytest.fixture(autouse=True)
def restore_process_umask():
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


class FakeExport:
    base_url = "https://backup.example.invalid"

    def __init__(self, tables=None, batch_size=2):
        self.tables = tables if tables is not None else {
            ("room", "messages"): [
                {"rowid": str(rowid), "data": {"id": index, "body": "preserved \u2603"}}
                for index, rowid in enumerate((-(2 ** 63), -1, 0, 2, 2 ** 63 - 1))
            ],
            ("hub", "messages"): [{"rowid": "1", "data": {"secret_hash": "fixture-only"}}],
            ("hub", "sqlite_sequence"): [],
        }
        self.batch_size = batch_size
        self.calls = []

    def get_json(self, path, parameters=None):
        self.calls.append((path, dict(parameters) if parameters else None))
        if path == "/admin/export/tables":
            return {"tables": [
                {"store": store, "name": table, "count": len(rows), "sql": f'CREATE TABLE "{table}"(id)'}
                for (store, table), rows in self.tables.items()
            ]}
        assert path == "/admin/export/page"
        store, table = parameters["store"], parameters["table"]
        after = int(parameters["after"]) if "after" in parameters else None
        rows = [row for row in self.tables[(store, table)] if after is None or int(row["rowid"]) > after]
        rows = rows[:min(parameters["limit"], self.batch_size)]
        return {"store": store, "table": table, "rows": rows,
                "next_after": rows[-1]["rowid"] if rows else None, "done": not rows}


def read_manifest(directory):
    return json.loads((directory / "manifest.json").read_text())


def file_snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def test_backup_pages_every_row_once_and_hashes_manifest(tmp_path):
    client = FakeExport(batch_size=2)
    directory = backup_live.backup(client, tmp_path / "backups", page_limit=2)
    manifest = read_manifest(directory)
    assert manifest["format_version"] == 1
    assert manifest["complete"] is True
    assert manifest["created_at"].endswith("Z")
    assert manifest["source"] == client.base_url
    assert len(manifest["tables"]) == 3
    filenames = set()
    for table in manifest["tables"]:
        data = (directory / table["file"]).read_bytes()
        expected = client.tables[(table["store"], table["table"])]
        assert json.loads(data) == expected
        assert table["expected_count"] == table["count"] == len(expected)
        assert table["sha256"] == hashlib.sha256(data).hexdigest()
        assert table["sql"].startswith("CREATE TABLE")
        assert table["file"] not in filenames
        filenames.add(table["file"])
        assert (directory / table["file"]).stat().st_mode & 0o777 == 0o600
    page_calls = [parameters for path, parameters in client.calls if path.endswith("/page")]
    for identity in client.tables:
        first = next(p for p in page_calls if (p["store"], p["table"]) == identity)
        assert "after" not in first  # Negative and zero rowids must not be skipped.
    assert directory.stat().st_mode & 0o777 == 0o700


def test_backup_continues_after_nonempty_short_pages(tmp_path):
    client = FakeExport(batch_size=1)
    directory = backup_live.backup(client, tmp_path, page_limit=1000)
    assert sum(table["count"] for table in read_manifest(directory)["tables"]) == 6
    assert len([call for call in client.calls if call[0].endswith("/page")]) == 9


def test_backup_retains_previous_runs_on_success_and_missing_page_failure(tmp_path):
    root = tmp_path / "backups"
    first = backup_live.backup(FakeExport(), root)
    first_files = file_snapshot(first)
    second = backup_live.backup(FakeExport(), root)
    second_files = file_snapshot(second)
    assert second != first

    class MissingPage(FakeExport):
        def get_json(self, path, parameters=None):
            result = super().get_json(path, parameters)
            if parameters and "after" in parameters:
                result.update(rows=[], next_after=None, done=True)
            return result

    with pytest.raises(backup_live.BackupError, match="row count mismatch"):
        backup_live.backup(MissingPage(), root)
    assert file_snapshot(first) == first_files
    assert file_snapshot(second) == second_files
    failed, = set(root.iterdir()) - {first, second}
    assert failed.is_dir()
    assert not (failed / "manifest.json").exists()
    assert list(failed.glob("*.json"))  # Partial data is retained, never cleaned up.


def test_backup_fails_on_any_page_error_without_a_manifest(tmp_path):
    class BrokenPage(FakeExport):
        def get_json(self, path, parameters=None):
            if parameters and "after" in parameters:
                raise backup_live.BackupError("export request failed with HTTP 503")
            return super().get_json(path, parameters)

    with pytest.raises(backup_live.BackupError, match="503"):
        backup_live.backup(BrokenPage(), tmp_path)
    directory, = tmp_path.iterdir()
    assert not (directory / "manifest.json").exists()


@pytest.mark.parametrize("change", ["count", "sql", "table"])
def test_backup_refuses_a_changed_final_inventory(tmp_path, change):
    class ChangingInventory(FakeExport):
        def get_json(self, path, parameters=None):
            result = super().get_json(path, parameters)
            if path.endswith("/tables") and len(self.calls) > 1:
                table = result["tables"][0]
                if change == "count":
                    table["count"] += 1
                elif change == "sql":
                    table["sql"] += "; -- changed"
                else:
                    table["name"] = "different-table"
            return result

    with pytest.raises(backup_live.BackupError, match="inventory changed"):
        backup_live.backup(ChangingInventory(), tmp_path)
    assert not list(tmp_path.glob("*/manifest.json"))


def test_backup_rejects_an_empty_inventory(tmp_path):
    with pytest.raises(backup_live.BackupError, match="empty or omits"):
        backup_live.backup(FakeExport(tables={}), tmp_path)


def test_backup_rejects_an_inventory_missing_one_store(tmp_path):
    with pytest.raises(backup_live.BackupError, match="empty or omits"):
        backup_live.backup(FakeExport(tables={("room", "only-room"): []}), tmp_path)


def test_backup_rejects_more_rows_than_advertised(tmp_path):
    class UnderreportedCount(FakeExport):
        def get_json(self, path, parameters=None):
            result = super().get_json(path, parameters)
            if path.endswith("/tables"):
                result["tables"][0]["count"] = 0
            return result

    with pytest.raises(backup_live.BackupError, match="more rows"):
        backup_live.backup(UnderreportedCount(), tmp_path)


@pytest.mark.parametrize("mutate", [
    lambda page: page.update(store="hub"),
    lambda page: page.update(table="wrong-table"),
    lambda page: page.update(next_after="7"),
    lambda page: page.update(done="false"),
    lambda page: page.update(rows=[], next_after=None, done=False),
    lambda page: page.update(rows=[{"rowid": "0", "data": {}}, {"rowid": "0", "data": {}}], next_after="0"),
    lambda page: page.update(rows=[{"rowid": "2", "data": {}}, {"rowid": "1", "data": {}}], next_after="1"),
    lambda page: page.update(rows=[{"rowid": 1, "data": {}}], next_after=1),
    lambda page: page.update(rows=[{"rowid": "9223372036854775808", "data": {}}], next_after="9223372036854775808"),
    lambda page: page.update(rows=[{"rowid": "01", "data": {}}], next_after="01"),
    lambda page: page.update(rows=[{"rowid": "1", "data": []}], next_after="1"),
    lambda page: page.update(rows=[{"rowid": str(i), "data": {}} for i in range(3)], next_after="2"),
])
def test_backup_rejects_malformed_pages(tmp_path, mutate):
    class MalformedPage(FakeExport):
        def get_json(self, path, parameters=None):
            result = super().get_json(path, parameters)
            if parameters:
                mutate(result)
            return result

    with pytest.raises(backup_live.BackupError):
        backup_live.backup(MalformedPage(), tmp_path, page_limit=2)
    assert not list(tmp_path.glob("*/manifest.json"))


def test_backup_rejects_a_cursor_regressing_between_pages(tmp_path):
    class RepeatedPage(FakeExport):
        def get_json(self, path, parameters=None):
            if parameters and "after" in parameters:
                parameters = {key: value for key, value in parameters.items() if key != "after"}
            return super().get_json(path, parameters)

    with pytest.raises(backup_live.BackupError, match="out of order"):
        backup_live.backup(RepeatedPage(), tmp_path)


@pytest.mark.parametrize("inventory", [
    [], {}, {"tables": {}}, {"tables": [None]},
    {"tables": [{"store": "other", "name": "t", "count": 0, "sql": "CREATE TABLE t(x)"}]},
    {"tables": [{"store": "room", "name": "t", "count": True, "sql": "CREATE TABLE t(x)"}]},
    {"tables": [{"store": "room", "name": "t", "count": -1, "sql": "CREATE TABLE t(x)"}]},
    {"tables": [{"store": "room", "name": "t", "count": 0, "sql": ""}]},
    {"tables": [{"store": "room", "name": "t", "count": 0, "sql": "CREATE TABLE t(x)"}] * 2},
])
def test_backup_rejects_invalid_or_duplicate_table_descriptors(tmp_path, inventory):
    class BadInventory(FakeExport):
        def get_json(self, path, parameters=None):
            return inventory

    with pytest.raises(backup_live.BackupError):
        backup_live.backup(BadInventory(), tmp_path)


def test_table_names_cannot_escape_or_collide(tmp_path):
    client = FakeExport({("room", "../../outside"): [], ("room", "a/b"): [],
                         ("room", "a_b"): [], ("hub", "a/b"): []})
    directory = backup_live.backup(client, tmp_path)
    names = [entry["file"] for entry in read_manifest(directory)["tables"]]
    assert len(names) == len(set(names)) == 4
    assert all("/" not in name and ".." not in name for name in names)
    assert set(tmp_path.iterdir()) == {directory}


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


def test_http_redirects_never_forward_credentials():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/should-not-be-requested")
            self.end_headers()

        def log_message(self, *args):
            pass

    with local_http(Handler) as origin:
        url = backup_live.validate_url(origin, allow_local_http=True)
        client = backup_live.ExportClient(url, "fixture-token-" * 3, delay=0)
        with pytest.raises(backup_live.BackupError, match="HTTP 302"):
            client.get_json("/admin/export/tables")
    assert calls == ["/admin/export/tables"]


@pytest.mark.parametrize("payload", [b"not json", b'{"tables":[],"tables":[]}', b'{"bad":NaN}'])
def test_http_client_rejects_corrupt_json(payload):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    with local_http(Handler) as origin:
        client = backup_live.ExportClient(origin, "fixture-token-" * 3, delay=0)
        with pytest.raises(backup_live.BackupError):
            client.get_json("/admin/export/tables")


def test_network_errors_do_not_expose_credentials(capsys, monkeypatch, tmp_path):
    secret = "do-not-print-this-fixture-secret-123456789"
    monkeypatch.setenv("ADMIN_EXPORT_TOKEN", secret)
    monkeypatch.setenv("AGENTMESH_URL", "https://backup.example.invalid")

    def failed_open(*args, **kwargs):
        raise URLError(secret)

    client = backup_live.ExportClient("https://backup.example.invalid", secret)
    monkeypatch.setattr(client.opener, "open", failed_open)
    with pytest.raises(backup_live.BackupError) as error:
        client.get_json("/admin/export/tables")
    assert secret not in str(error.value)
    monkeypatch.setattr(backup_live, "build_opener", lambda *args: client.opener)
    assert backup_live.main(["--tokens-file", str(tmp_path / "absent.conf"),
                             "--backup-dir", str(tmp_path / "backups")]) == 1
    output = capsys.readouterr()
    assert "Backup failed" in output.err
    assert secret not in output.out + output.err


def test_backup_cli_pulls_only_local_pages_with_private_headers(tmp_path, monkeypatch, capsys):
    fixture = FakeExport(batch_size=1)
    headers = []
    token = "local-fixture-owner-token-1234567890"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            headers.append((self.headers.get("Authorization"), self.headers.get("CF-Access-Client-Id"),
                            self.headers.get("CF-Access-Client-Secret")))
            parts = urlsplit(self.path)
            parameters = {key: values[0] for key, values in parse_qs(parts.query).items()}
            if parameters:
                parameters["limit"] = int(parameters["limit"])
            payload = json.dumps(fixture.get_json(parts.path, parameters or None)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    monkeypatch.setenv("ADMIN_EXPORT_TOKEN", token)
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "local-access-id")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "local-access-secret")
    with local_http(Handler) as origin:
        result = backup_live.main(["--url", origin, "--allow-local-http", "--request-delay", "0",
                                   "--tokens-file", str(tmp_path / "absent.conf"),
                                   "--backup-dir", str(tmp_path / "backups")])
    assert result == 0
    assert all(header == ("Bearer " + token, "local-access-id", "local-access-secret") for header in headers)
    output = capsys.readouterr()
    assert "Backup complete:" in output.out
    assert token not in output.out + output.err
    directory, = (tmp_path / "backups").iterdir()
    assert sum(entry["count"] for entry in read_manifest(directory)["tables"]) == 6


def test_private_configuration_is_parsed_not_executed(tmp_path):
    marker = tmp_path / "must-not-exist"
    token_file = tmp_path / "tokens.conf"
    token_file.write_text('ADMIN_EXPORT_TOKEN="' + "a" * 40 + '"\n'
                          'AGENTMESH_URL=https://backup.example.invalid\n'
                          f'IGNORED=$(touch {marker})\n')
    token_file.chmod(0o600)
    settings = backup_live.load_settings(token_file, {"ADMIN_EXPORT_TOKEN": "b" * 40})
    assert settings["ADMIN_EXPORT_TOKEN"] == "b" * 40
    assert not marker.exists()
    token_file.chmod(0o644)
    with pytest.raises(backup_live.BackupError, match="0600"):
        backup_live.load_settings(token_file, {})


@pytest.mark.parametrize("url", ["http://backup.example.invalid", "http://127.0.0.1",
                                  "https://secret@backup.example.invalid", "https://backup.example.invalid/?token=secret",
                                  "https://backup.example.invalid/path", "https://backup.example.invalid/#secret"])
def test_credentials_require_secure_origin(url):
    with pytest.raises(backup_live.BackupError):
        backup_live.validate_url(url)
    if "127.0.0.1" not in url:
        with pytest.raises(backup_live.BackupError):
            backup_live.validate_url(url, allow_local_http=True)


def test_timer_installs_daily_units_without_reading_or_printing_tokens(tmp_path):
    home, binary = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    binary.mkdir()
    log = tmp_path / "systemctl-calls"
    fake_systemctl = binary / "systemctl"
    fake_systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_TEST_LOG"\n')
    fake_systemctl.chmod(0o700)
    project = tmp_path / 'project % $name "quoted"'
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "backup_live.py").write_text("# fixture only\n")
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/install_backup_timer.sh"), "--project", str(project),
         "--backup-dir", str(home / "all backups")],
        env={"HOME": str(home), "PATH": str(binary) + ":/usr/bin:/bin", "SYSTEMCTL_TEST_LOG": str(log)},
        capture_output=True, text=True, check=True,
    )
    units = home / ".config/systemd/user"
    timer = (units / "agentmesh-backup.timer").read_text()
    service = (units / "agentmesh-backup.service").read_text()
    assert "OnCalendar=daily" in timer and "Persistent=true" in timer
    assert "Type=oneshot" in service and "UMask=0077" in service and "TimeoutStartSec=0" in service
    assert "%%" in service and "$$name" in service and '\\"quoted\\"' in service
    assert "EnvironmentFile=" not in service  # Python parses the private file, not a shell.
    assert not (home / ".config/agentmesh/tokens.env").exists()
    assert "daemon-reload" in log.read_text() and "enable --now agentmesh-backup.timer" in log.read_text()
    assert "ADMIN_EXPORT_TOKEN=" not in completed.stdout + completed.stderr + service
