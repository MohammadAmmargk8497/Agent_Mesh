#!/usr/bin/env python3
"""Restore both Worker stores to their oldest supported time, then merge a backup.

This is an owner-operated, mutating command, not an automatic deployment step.
Backups are opened read-only, checked in full before restoring, and never changed.
Use --dry-run to validate the backup and resolve restore times without restoring
or importing. Credentials use the same private configuration as backup_live.py.
"""

import argparse
import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

import backup_live

BackupError = backup_live.BackupError
IMPORT_BYTES = 2 * 1024 * 1024
STORES = ("room", "hub")


class RestoreClient(backup_live.ExportClient):
    def post_json(self, path, body=None, parameters=None):
        if self._last_request is not None:
            time.sleep(max(0, self.delay - (time.monotonic() - self._last_request)))
        url = self.base_url + path
        if parameters:
            url += "?" + urlencode(parameters)
        payload = None if body is None else _encode(body)
        request = Request(url, data=payload, headers={**self.headers, "Content-Type": "application/json"}, method="POST")
        self._last_request = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise BackupError("admin request returned a non-200 response")
                data = response.read(backup_live.MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise BackupError(f"admin request failed with HTTP {error.code}; operation stopped") from None
        except (URLError, OSError, ValueError, HTTPException):
            # Do not retry a restore: a disconnected request may have succeeded.
            raise BackupError("admin network request failed; restore was not retried") from None
        if len(data) > backup_live.MAX_RESPONSE_BYTES:
            raise BackupError("admin response exceeds the safe size")
        return _decode(data)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _decode(data):
    try:
        return json.loads(data, object_pairs_hook=backup_live._unique_object,
                          parse_constant=backup_live._invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise BackupError("invalid backup or admin JSON") from None


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _schema(name, sql):
    # Inspect a single CREATE statement in memory, never execute backup SQL on
    # disk or against the live API. In particular, no executescript/ATTACH/UDFs.
    if not re.match(r"\s*CREATE\s+TABLE\s", sql, re.IGNORECASE):
        raise BackupError("backup schema must be a CREATE TABLE statement")
    with closing(sqlite3.connect(":memory:")) as db:
        db.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in (
            sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_CREATE_VTABLE,
        ) else sqlite3.SQLITE_OK)
        try:
            if name == "sqlite_sequence":
                # SQLite reserves this name; create its authentic schema using
                # an in-memory AUTOINCREMENT table rather than rename metadata.
                if sql.strip().rstrip(";").lower() != "create table sqlite_sequence(name,seq)":
                    raise BackupError("unexpected sqlite_sequence schema")
                db.execute("CREATE TABLE sequence_probe(id INTEGER PRIMARY KEY AUTOINCREMENT)")
            else:
                db.execute(sql)
            db.set_authorizer(None)
            columns = db.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
            dependencies = {row[2] for row in db.execute(f"PRAGMA foreign_key_list({_quote(name)})")}
        except sqlite3.Error:
            raise BackupError("invalid or unsupported table schema") from None
    if not columns or any(row[1].lower() in ("rowid", "__agentmesh_export_rowid") for row in columns):
        raise BackupError("missing table schema or shadowed export rowid")
    primary_key = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
    # sqlite_sequence has neither a declared key nor a uniqueness constraint.
    # Treat its table name as identity; never append a duplicate high-water row.
    if name == "sqlite_sequence":
        primary_key = ("name",)
    return {"columns": tuple(row[1] for row in columns), "primary_key": primary_key,
            "dependencies": dependencies}


def _cell(value):
    if value is None or isinstance(value, str):
        return value
    if type(value) in (int, float):
        if ((type(value) is int and abs(value) > 2 ** 53 - 1)
                or (type(value) is float and (not math.isfinite(value)
                    or (value.is_integer() and abs(value) > 2 ** 53 - 1)))):
            raise BackupError("unsafe numeric backup cell")
        return value
    if isinstance(value, dict) and set(value) == {"$base64"} and isinstance(value["$base64"], str):
        try:
            decoded = base64.b64decode(value["$base64"], validate=True)
            if base64.b64encode(decoded).decode("ascii") == value["$base64"]:
                return decoded
        except (ValueError, binascii.Error):
            pass
    raise BackupError("unsupported backup cell")


def _row_key(table, row):
    if set(row["data"]) != set(table["columns"]):
        raise BackupError("row columns do not match the table schema")
    values = {name: _cell(value) for name, value in row["data"].items()}
    key = tuple(values[name] for name in table["primary_key"])
    # SQLite permits NULL in some declared primary keys. Such a row's only
    # unambiguous identity (as for keyless tables) is its original rowid.
    return ("pk", key) if key and all(value is not None for value in key) else ("rowid", row["rowid"])


def _manifest(directory, allow_incomplete=False):
    path = directory / "manifest.json"
    if path.is_symlink():
        raise BackupError("backup manifests must not be symlinks")
    document = _decode(path.read_bytes())
    if allow_incomplete and isinstance(document, dict) and document.get("complete") is not True:
        return None, None
    if (not isinstance(document, dict) or document.get("complete") is not True
            or type(document.get("format_version")) is not int or document["format_version"] != 1):
        raise BackupError("backup manifest must be complete and format version 1")
    try:
        created = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
        if created.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BackupError("invalid manifest creation time") from None
    backup_live.validate_url(document.get("source"), allow_local_http=True)
    return document, created


def _select_backup(path):
    directory = Path(path).expanduser()
    if directory.is_symlink():
        raise BackupError("backup directories must not be symlinks")
    if (directory / "manifest.json").exists():
        return directory
    candidates = []
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_dir() or not (child / "manifest.json").is_file():
            continue
        document, created = _manifest(child, allow_incomplete=True)
        if document is None:
            continue
        candidates.append((created, child.name, child))
    if not candidates:
        raise BackupError("no complete backup manifest found")
    return max(candidates)[2]


def load_backup(path):
    directory = _select_backup(path)
    manifest, _ = _manifest(directory)
    entries = manifest.get("tables")
    if not isinstance(entries, list):
        raise BackupError("invalid manifest tables")
    backup_live._tables({"tables": [
        {"store": item.get("store"), "name": item.get("table"), "count": item.get("count"), "sql": item.get("sql")}
        if isinstance(item, dict) else item for item in entries
    ]})
    tables, filenames = [], set()
    for entry in entries:
        filename = entry.get("file")
        if (not isinstance(filename, str) or not filename or filename in (".", "..", "manifest.json")
                or Path(filename).name != filename or "\\" in filename or filename in filenames):
            raise BackupError("unsafe or duplicate backup filename")
        filenames.add(filename)
        file = directory / filename
        if file.is_symlink() or not file.is_file():
            raise BackupError("backup table must be a regular non-symlink file")
        payload = file.read_bytes()
        if (not isinstance(entry.get("sha256"), str)
                or hashlib.sha256(payload).hexdigest() != entry["sha256"]):
            raise BackupError("backup table SHA-256 mismatch")
        rows = _decode(payload)
        if (not isinstance(rows, list) or type(entry.get("expected_count")) is not int
                or entry["expected_count"] != entry["count"] or len(rows) != entry["count"]):
            raise BackupError("backup table row count mismatch")
        table = {**entry, **_schema(entry["table"], entry["sql"]), "rows": rows,
                 "source": manifest["source"], "directory": directory}
        backup_live._page({"store": entry["store"], "table": entry["table"], "rows": rows,
                           "next_after": rows[-1].get("rowid") if rows and isinstance(rows[-1], dict) else None,
                           "done": True}, entry["store"], entry["table"], None, max(1, len(rows)))
        keys = [_row_key(table, row) for row in rows]
        if len(set(keys)) != len(keys):
            raise BackupError("duplicate backup primary key")
        # Check pageability before either store is rolled back.
        for _ in _batches(rows, 1000):
            pass
        tables.append(table)
    return tables


def _inventory(client, tables, document=None):
    inventory = {(item["store"], item["name"]): item
                 for item in backup_live._tables(document if document is not None else client.get_json("/admin/export/tables"))}
    for table in tables:
        live = inventory.get((table["store"], table["table"]))
        if live is None:
            raise BackupError("a backup table is missing from the live schema")
        schema = _schema(live["name"], live["sql"])
        if any(table[key] != schema[key] for key in ("columns", "primary_key")):
            raise BackupError("backup and live table schemas do not match")
    return inventory


def _export_rows(client, table, page_limit):
    after = None
    while True:
        parameters = {"store": table["store"], "table": table["table"], "limit": page_limit}
        if after is not None:
            parameters["after"] = after
        rows, after, done = backup_live._page(client.get_json("/admin/export/page", parameters),
                                            table["store"], table["table"], after, page_limit)
        yield from rows
        if done:
            break


def _batches(rows, page_limit):
    batch, size = [], len(b'{"rows":[]}')
    for row in rows:
        length = len(_encode(row))
        if length + len(b'{"rows":[]}') > IMPORT_BYTES:
            raise BackupError("single backup row exceeds import byte limit")
        if batch and (len(batch) >= page_limit or size + 1 + length > IMPORT_BYTES):
            yield batch
            batch, size = [], len(b'{"rows":[]}')
        size += length + bool(batch)
        batch.append(row)
    if batch:
        yield batch


def _ordered_tables(tables):
    pending = {(table["store"], table["table"]): table for table in tables}
    result = []
    while pending:
        ready = [identity for identity, table in pending.items()
                 if not any((table["store"], name) in pending and name != table["table"]
                            for name in table["dependencies"])]
        if not ready:
            raise BackupError("cyclic backup foreign keys require manual review")
        for identity in ready:
            result.append(pending.pop(identity))
    # AUTOINCREMENT inserts can create sqlite_sequence entries themselves;
    # leave existing high-water rows unchanged and add only names still absent.
    return sorted(result, key=lambda table: table["table"] == "sqlite_sequence")


def merge_backup(client, tables, page_limit=1000):
    if type(page_limit) is not int or not 1 <= page_limit <= 1000:
        raise BackupError("page limit must be between 1 and 1000")
    _inventory(client, tables)
    totals = {"inserted": 0, "skipped": 0}
    for table in _ordered_tables(tables):
        existing = {_row_key(table, row) for row in _export_rows(client, table, page_limit)}
        missing = [row for row in table["rows"] if _row_key(table, row) not in existing]
        for rows in _batches(missing, page_limit):
            result = client.post_json("/admin/import", {"rows": rows},
                                      {"store": table["store"], "table": table["table"]})
            if (not isinstance(result, dict)
                    or any(type(result.get(key)) is not int or result[key] < 0 for key in totals)
                    or result["inserted"] + result["skipped"] != len(rows)):
                raise BackupError("invalid import accounting")
            for key in totals:
                totals[key] += result[key]
        print(f"MERGE {table['store']}/{table['table']}: missing={len(missing)} existing={len(table['rows']) - len(missing)}")
    return totals


def verify_backup(client, tables, page_limit=1000):
    if type(page_limit) is not int or not 1 <= page_limit <= 1000:
        raise BackupError("page limit must be between 1 and 1000")
    try:
        inventory = {(item["store"], item["name"]): item
                     for item in backup_live._tables(client.get_json("/admin/export/tables"))}
    except BackupError:
        for table in tables:
            print(f"FAIL {table['store']}/{table['table']}: export inventory unavailable")
        return False
    passed = True
    for table in tables:
        identity = (table["store"], table["table"])
        try:
            live = inventory.get(identity)
            if live is None:
                raise BackupError("backup table missing from live export")
            schema = _schema(live["name"], live["sql"])
            if any(table[key] != schema[key] for key in ("columns", "primary_key")):
                raise BackupError("backup and live schema mismatch")
            rows = list(_export_rows(client, table, page_limit))
            keys = {_row_key(table, row) for row in rows}
            missing = sum(_row_key(table, row) not in keys for row in table["rows"])
            ok = not missing and len(rows) >= table["count"] and live["count"] == len(rows)
            detail = f"backup={table['count']} live={len(rows)} missing={missing}"
        except BackupError as error:
            ok, detail = False, str(error)
        print(f"{'PASS' if ok else 'FAIL'} {identity[0]}/{identity[1]}: {detail}")
        passed = passed and ok
    return passed


def _restore_result(result, store, dry_run):
    if (not isinstance(result, dict) or result.get("store") != store
            or result.get("dry_run") is not dry_run or not isinstance(result.get("bookmark"), str)
            or not result["bookmark"] or type(result.get("time")) is not int or result["time"] < 0):
        raise BackupError("invalid restore result")
    stamp = datetime.fromtimestamp(result["time"] / 1000).astimezone().isoformat()
    print(f"{'DRY RUN' if dry_run else 'RESTORE'} {store}: time={result['time']} ({stamp}) bookmark={result['bookmark']}")
    return result


def _wait_exports(client, tables, wait_timeout):
    deadline = time.monotonic() + wait_timeout
    while True:
        try:
            document = client.get_json("/admin/export/tables")
        except BackupError:
            if time.monotonic() >= deadline:
                raise BackupError("timed out waiting for exports after restore; no restore retried") from None
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        else:
            # Schema differences are permanent, not a reason to poll or seed a
            # restored object through the normal mutable application routes.
            _inventory(client, tables, document)
            return


def rollback_merge(client, backup_path, page_limit=1000, wait_timeout=120, dry_run=False):
    tables = load_backup(backup_path)
    if any(table["source"].rstrip("/") != client.base_url.rstrip("/") for table in tables):
        raise BackupError("backup source does not match the configured AgentMesh origin")
    if type(page_limit) is not int or not 1 <= page_limit <= 1000:
        raise BackupError("page limit must be between 1 and 1000")
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise BackupError("wait timeout must be finite and positive")
    _ordered_tables(tables)  # Reject dependency cycles before scheduling restore.
    _inventory(client, tables)
    print(f"Validated complete backup: {tables[0]['directory']}; {len(tables)} tables")
    bookmarks = {store: _restore_result(client.post_json("/admin/restore", parameters={
        "store": store, "time": 0, "dry_run": "1",
    }), store, True) for store in STORES}
    if dry_run:
        print("Dry run complete: no restore or import requested; local files unchanged")
        return True
    for store in STORES:
        _restore_result(client.post_json("/admin/restore", parameters={
            "store": store, "time": bookmarks[store]["time"], "dry_run": "0",
        }), store, False)
        # The hub participates in owner authorization of *both* stores. Do not
        # start another restore while a prior instance is still aborting.
        _wait_exports(client, tables, wait_timeout)
    totals = merge_backup(client, tables, page_limit)
    passed = verify_backup(client, tables, page_limit)
    print(f"Summary: {'PASS' if passed else 'FAIL'}; inserted={totals['inserted']} skipped={totals['skipped']}; local files unchanged")
    return passed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=Path.home() / "agentmesh-backups",
                        help="a completed run, or root from which the latest completed run is selected")
    parser.add_argument("--url", help="HTTPS AgentMesh origin (default: AGENTMESH_URL)")
    parser.add_argument("--tokens-file", type=Path, default=Path.home() / ".config/agentmesh/tokens.env")
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--request-delay", type=float, default=0.55)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--wait-timeout", type=float, default=120)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-local-http", action="store_true", help="loopback fixtures only")
    args = parser.parse_args(argv)
    try:
        if (not math.isfinite(args.request_delay) or args.request_delay < 0
                or not math.isfinite(args.timeout) or args.timeout <= 0):
            raise BackupError("request delay must be finite and nonnegative; timeout must be finite and positive")
        settings = backup_live.load_settings(args.tokens_file)
        url = backup_live.validate_url(args.url or settings.get("AGENTMESH_URL", ""), args.allow_local_http)
        client = RestoreClient(url, settings["ADMIN_EXPORT_TOKEN"], settings.get("CF_ACCESS_CLIENT_ID", ""),
                               settings.get("CF_ACCESS_CLIENT_SECRET", ""), args.timeout, args.request_delay)
        return 0 if rollback_merge(client, args.backup_dir, args.page_limit, args.wait_timeout, args.dry_run) else 1
    except BackupError as error:
        print("Rollback/merge failed: " + str(error), file=sys.stderr)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, sqlite3.Error):
        print("Rollback/merge failed: local I/O or invalid data; no local files were changed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
