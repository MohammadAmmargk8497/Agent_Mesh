#!/usr/bin/env python3
"""Import a complete owner backup into the local server's two SQLite files.

Stop the local server first. --replace permits an insert-only merge into
non-empty databases, including an idempotent rerun; it never replaces, updates
or deletes existing rows. This command is offline and never reads credentials.
"""

import argparse
import os
import re
import sqlite3
import sys
from contextlib import ExitStack, closing
from pathlib import Path

import rollback_merge

BackupError = rollback_merge.BackupError
quote = rollback_merge._quote
SQL_TOKENS = re.compile(
    r"--[^\r\n]*|/\*.*?(?:\*/|$)|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]"
    r"|[\w$]+|(?:\|\||<>|!=|==|<=|>=|<<|>>|->>|->)|[^\s]", re.DOTALL,
)
ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _normalized_sql(sql):
    # Token boundaries survive whitespace/comment removal. Quoted strings and
    # identifiers stay byte-for-byte intact; only unquoted ASCII case folds.
    # Equivalent alternative syntax may be rejected deliberately: never guess
    # that different UNIQUE/CHECK/FK/collation/conflict semantics are harmless.
    return tuple(token if token[:1] in ("'", '"', "`", "[") else token.translate(ASCII_UPPER)
                 for match in SQL_TOKENS.finditer(sql)
                 if not (token := match.group()).startswith(("--", "/*")))


def _schema_details(sql, name):
    with closing(sqlite3.connect(":memory:")) as db:
        if name == "sqlite_sequence":
            db.execute("CREATE TABLE sequence_probe(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        else:
            db.execute(sql)
        columns = db.execute(f"PRAGMA table_info({quote(name)})").fetchall()
        canonical = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]
        return columns, _normalized_sql(canonical)


def _safe_path(directory, name):
    path = directory / name
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"),
                      Path(str(path) + "-journal")):
        if candidate.is_symlink():
            raise BackupError("database files must not be symlinks")
    if path.exists() and not path.is_file():
        raise BackupError("database must be a regular file")
    return path


def _key_condition(table, row):
    values = {name: rollback_merge._cell(value) for name, value in row["data"].items()}
    primary = table["primary_key"]
    if primary and all(values[name] is not None for name in primary):
        return " AND ".join(quote(name) + " IS ?" for name in primary), [values[name] for name in primary]
    return "rowid = ?", [int(row["rowid"])]


def _exists(db, table, row):
    condition, values = _key_condition(table, row)
    return db.execute("SELECT 1 FROM " + quote(table["table"]) + " WHERE " + condition, values).fetchone() is not None


def _insert_only(action, _first, _second, _database, source):
    # Reject trigger side effects as well as direct updates/deletes. SQLite's
    # internal AUTOINCREMENT bookkeeping is not an application UPDATE.
    if source is not None or action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def import_backup(backup_dir, config_dir, replace=False):
    """Validate everything first, then insert missing keys; existing values win."""
    tables = rollback_merge.load_backup(backup_dir)
    ordered = rollback_merge._ordered_tables(tables)
    expected_schemas = {(table["store"], table["table"]): _schema_details(table["sql"], table["table"])
                        for table in tables}
    directory = Path(config_dir).expanduser()
    if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):
        raise BackupError("database directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = {store: _safe_path(directory, name) for store, name in (("room", "data.db"), ("hub", "hub.db"))}
    results = []
    with ExitStack() as stack:
        databases = {}
        for store, path in paths.items():
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            db = stack.enter_context(closing(sqlite3.connect(path)))
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            databases[store] = db
            populated = any(db.execute("SELECT 1 FROM " + quote(name) + " LIMIT 1").fetchone()
                            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            if populated and not replace:
                raise BackupError("non-empty databases require --replace (insert-only merge; nothing is deleted)")
        for table in ordered:
            db, name = databases[table["store"]], table["table"]
            existing = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            if existing is None:
                if name == "sqlite_sequence":
                    raise BackupError("sqlite_sequence requires an AUTOINCREMENT table in the backup")
                # load_backup has already checked this single CREATE statement
                # in an isolated database; executescript is deliberately unused.
                db.execute(table["sql"])
                existing = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            columns = db.execute(f"PRAGMA table_info({quote(name)})").fetchall()
            if (columns, _normalized_sql(existing[0])) != expected_schemas[(table["store"], name)]:
                raise BackupError("backup and local table schemas do not match")
            foreign_keys = {row[2] for row in db.execute(f"PRAGMA foreign_key_list({quote(name)})")}
            if foreign_keys != table["dependencies"]:
                raise BackupError("backup and local foreign keys do not match")

        # Schema creation has made sqlite_sequence available. Import its saved
        # high-water marks before AUTOINCREMENT inserts can generate lower ones.
        ordered = sorted(ordered, key=lambda table: table["table"] != "sqlite_sequence")
        for table in ordered:
            db, name = databases[table["store"]], table["table"]
            before = db.execute("SELECT COUNT(*) FROM " + quote(name)).fetchone()[0]
            inserted = 0
            db.set_authorizer(_insert_only)
            try:
                for row in table["rows"]:
                    if _exists(db, table, row):
                        continue
                    columns = list(table["columns"])
                    values = [rollback_merge._cell(row["data"][column]) for column in columns]
                    if not table["primary_key"] or any(row["data"][key] is None for key in table["primary_key"]):
                        columns.insert(0, "rowid")
                        values.insert(0, int(row["rowid"]))
                    db.execute("INSERT OR IGNORE INTO " + quote(name) + " (" + ",".join(map(quote, columns))
                               + ") VALUES (" + ",".join("?" for _ in columns) + ")", values)
                    inserted += db.execute("SELECT changes()").fetchone()[0]
            finally:
                db.set_authorizer(None)
            matched = sum(_exists(db, table, row) for row in table["rows"])
            total = db.execute("SELECT COUNT(*) FROM " + quote(name)).fetchone()[0]
            if matched != table["count"] or total < table["count"] or (before == 0 and total != table["count"]):
                raise BackupError("imported backup keys/counts do not match the manifest")
            results.append({"store": table["store"], "table": name, "expected": table["count"],
                            "matched": matched, "inserted": inserted, "skipped": len(table["rows"]) - inserted,
                            "total": total})

        # The Worker stores this version in a table; Node's unchanged OpenHuddle
        # adapter uses user_version. Preserve the table and copy its version.
        hub = databases["hub"]
        if any(table["store"] == "hub" and table["table"] == "agentmesh_schema" for table in tables):
            row = hub.execute("SELECT version FROM agentmesh_schema WHERE id = 1").fetchone()
            if row is None or type(row[0]) is not int or not 1 <= row[0] <= 2 ** 31 - 1:
                raise BackupError("invalid imported hub schema version")
            current = hub.execute("PRAGMA user_version").fetchone()[0]
            hub.execute("PRAGMA user_version = " + str(max(current, row[0])))
        # Table-only Worker exports omit indexes. Setting user_version skips
        # Node's migrations, so recreate its two indexes from trusted local SQL.
        for name, table, columns in (("members_team", "members", ("team_id",)),
                                     ("events_team", "events", ("team_id", "id"))):
            available = {column[1] for column in hub.execute(f"PRAGMA table_info({quote(table)})")}
            if set(columns) <= available:
                hub.execute("CREATE INDEX IF NOT EXISTS " + quote(name) + " ON " + quote(table)
                            + " (" + ",".join(map(quote, columns)) + ")")
        for db in databases.values():
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("local database contains a foreign-key violation")
        # Both stores have passed validation before either is committed. A disk
        # failure during commit may still require an insert-only rerun.
        for db in databases.values():
            db.commit()
    for result in results:
        print("PASS {store}/{table}: matched={matched} expected={expected} inserted={inserted} "
              "skipped={skipped} total={total}".format(**result))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", required=True, type=Path, help="complete backup run (or root selecting newest complete run)")
    parser.add_argument("--config-dir", type=Path,
                        default=Path(os.environ.get("AGENTMESH_SERVER_DIR") or Path.home() / ".config/agentmesh-server"),
                        help="local server directory containing data.db and hub.db")
    parser.add_argument("--replace", action="store_true",
                        help="allow insert-only merge into non-empty DBs/reruns; NEVER replace, update or delete rows")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        results = import_backup(args.backup_dir, args.config_dir, args.replace)
    except BackupError as error:
        print("Import failed: " + str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError):
        print("Import failed: local I/O, incompatible schema or invalid backup; no rows were deleted", file=sys.stderr)
        return 1
    print("Import complete: inserted=" + str(sum(result["inserted"] for result in results))
          + " skipped=" + str(sum(result["skipped"] for result in results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
