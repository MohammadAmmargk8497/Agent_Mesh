"""Shared validation for the versioned Worker SQLite export format."""

import base64
import binascii
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

DATABASES = {"room": "data.db", "hub": "hub.db"}


class ExportError(ValueError):
    """The export or destination cannot be used without risking data loss."""


def quote(name):
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ExportError("invalid SQLite identifier")
    return '"' + name.replace('"', '""') + '"'


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("duplicate JSON object key")
        result[key] = value
    return result


def decode_value(value):
    if isinstance(value, dict) and set(value) == {"$blob"} and isinstance(value["$blob"], str):
        try:
            return base64.b64decode(value["$blob"], validate=True)
        except (ValueError, binascii.Error) as error:
            raise ExportError("invalid base64 BLOB") from error
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise ExportError("SQLite values cannot be JSON booleans")
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ExportError("invalid SQLite value")


def load_export(path):
    with Path(path).open(encoding="utf-8") as stream:
        payload = json.load(stream, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or payload.get("format") != "agentmesh-sqlite-export" or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ExportError("expected agentmesh-sqlite-export version 1")
    databases = payload.get("databases")
    if not isinstance(databases, dict) or set(databases) != set(DATABASES):
        raise ExportError("export must contain exactly the room and hub databases")
    for database in databases.values():
        if not isinstance(database, dict) or not isinstance(database.get("tables"), dict) or not isinstance(database.get("schema"), list):
            raise ExportError("database must contain tables and schema")
        for name, table in database["tables"].items():
            quote(name)
            if not isinstance(table, dict):
                raise ExportError("invalid table export")
            sql = table.get("sql")
            if (not isinstance(sql, str) or not sql.strip()) and (name != "sqlite_sequence" or sql is not None):
                raise ExportError("table requires its CREATE TABLE SQL")
            columns = table.get("columns")
            rows = table.get("rows")
            if not isinstance(columns, list) or not columns or not isinstance(rows, list):
                raise ExportError("table requires columns and rows")
            for column in columns:
                quote(column)
            if len(set(columns)) != len(columns):
                raise ExportError("duplicate table column")
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(columns):
                    raise ExportError("export row does not match its table columns")
                for value in row.values():
                    decode_value(value)
        names = set()
        for item in database["schema"]:
            if not isinstance(item, dict) or item.get("type") not in {"index", "trigger", "view"}:
                raise ExportError("invalid schema object")
            quote(item.get("name"))
            quote(item.get("tbl_name"))
            key = (item["type"], item["name"])
            if key in names:
                raise ExportError("duplicate schema object name")
            names.add(key)
            if not isinstance(item.get("sql"), str) or not item["sql"].strip():
                raise ExportError("schema object requires its CREATE SQL")
    return payload


def safe_directory(directory):
    directory = Path(directory).absolute()
    for component in (directory, *directory.parents):
        if component.is_symlink():
            raise ExportError("data directory must not contain symbolic links")
    if directory.exists() and not directory.is_dir():
        raise ExportError("data directory is not a directory")
    return directory


def safe_target(path, *, importing=False):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ExportError("database must be a regular file, not a symbolic or hard link")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink() or (importing and sidecar.exists()):
            raise ExportError("database sidecar exists; stop Docker and checkpoint SQLite before importing")


def open_readonly(path):
    if not path.is_file():
        raise ExportError(f"missing database: {path.name}")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def table_counts(database):
    tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {name: database.execute(f"SELECT COUNT(*) FROM {quote(name)}").fetchone()[0]
            for (name,) in tables}


def existing_counts(path):
    with closing(open_readonly(path)) as database:
        return table_counts(database)
