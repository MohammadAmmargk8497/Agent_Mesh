"""Import both Worker databases offline: python -m agentmesh_server.scripts.import_export."""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from ._sqlite_export import (
    DATABASES,
    ExportError,
    decode_value,
    existing_counts,
    load_export,
    quote,
    safe_directory,
    safe_target,
    table_counts,
)


def _stage_database(path, source, *, hub=False):
    # DDL comes from the owner-supplied export, but must never access other files.
    forbidden = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                 sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE}
    with closing(sqlite3.connect(path)) as database:
        database.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                                if action in forbidden else sqlite3.SQLITE_OK)
        database.execute("PRAGMA foreign_keys=OFF")
        database.execute("BEGIN")
        tables = source["tables"]
        for name, table in tables.items():
            if name != "sqlite_sequence":
                database.execute(table["sql"])
        actual = table_counts(database)
        expected = set(tables)
        if "sqlite_sequence" in expected and "sqlite_sequence" not in actual:
            # sqlite_sequence survives even when its final AUTOINCREMENT table
            # has been dropped on the source. SQLite won't let us CREATE it.
            temporary = "_agentmesh_sequence_bootstrap"
            while temporary in tables:
                temporary += "_"
            database.execute(f"CREATE TABLE {quote(temporary)} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            database.execute(f"DROP TABLE {quote(temporary)}")
        if set(table_counts(database)) != expected:
            raise ExportError("CREATE TABLE SQL does not match the exported table set")
        for name, table in tables.items():
            columns = table["columns"]
            metadata = database.execute(f"PRAGMA table_xinfo({quote(name)})").fetchall()
            visible = [column[1] for column in metadata if column[6] != 1]
            if visible != columns:
                raise ExportError("CREATE TABLE SQL does not match exported columns")
            if name == "sqlite_sequence":
                continue
            # Generated columns are exported by SELECT * but are recomputed by
            # SQLite rather than inserted. Check their values after import.
            writable = [column[1] for column in metadata if column[6] == 0]
            query = (f"INSERT INTO {quote(name)} ({','.join(map(quote, writable))}) "
                     f"VALUES ({','.join('?' for _ in writable)})")
            database.executemany(query, ([decode_value(row[column]) for column in writable]
                                         for row in table["rows"]))
            if len(writable) != len(columns):
                restored = database.execute(f"SELECT * FROM {quote(name)}").fetchall()
                wanted = [tuple(decode_value(row[column]) for column in columns) for row in table["rows"]]
                if restored != wanted:
                    raise ExportError("generated column values differ from export")
        if "sqlite_sequence" in tables:
            database.execute("DELETE FROM sqlite_sequence")
            table = tables["sqlite_sequence"]
            columns = table["columns"]
            database.executemany(
                f"INSERT INTO sqlite_sequence ({','.join(map(quote, columns))}) VALUES ({','.join('?' for _ in columns)})",
                ([decode_value(row[column]) for column in columns] for row in table["rows"]))
        # INSTEAD OF triggers require their view to exist already, even if the
        # source listed triggers before views in sqlite_master.
        for item in sorted(source["schema"], key=lambda item: item["type"] != "view"):
            database.execute(item["sql"])
        actual_schema = {(kind, name, table) for kind, name, table in database.execute(
            "SELECT type,name,tbl_name FROM sqlite_master WHERE type != 'table' AND sql IS NOT NULL")}
        expected_schema = {(item["type"], item["name"], item["tbl_name"]) for item in source["schema"]}
        if actual_schema != expected_schema:
            raise ExportError("CREATE SQL does not match exported indexes, triggers and views")
        if hub and "agentmesh_schema" in tables:
            versions = database.execute("SELECT id,version FROM agentmesh_schema").fetchall()
            if len(versions) != 1 or versions[0][0] != 1 or type(versions[0][1]) is not int or not 0 <= versions[0][1] < 2**31:
                raise ExportError("invalid HubRoom schema version")
            # DO SQLite cannot use user_version, whereas the vendored Docker
            # hub uses this PRAGMA to decide which migrations to run at boot.
            database.execute(f"PRAGMA user_version={versions[0][1]}")
        if table_counts(database) != {name: len(table["rows"]) for name, table in tables.items()}:
            raise ExportError("staged row counts do not match export")
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ExportError("staged database integrity check failed")
        database.commit()
    path.chmod(0o600)


def import_export(export_path, data_dir, *, replace=False):
    """Stage and validate both files before replacing either stopped database."""
    payload = load_export(export_path)
    directory = safe_directory(data_dir)
    targets = {name: directory / filename for name, filename in DATABASES.items()}
    for path in targets.values():
        safe_target(path, importing=True)
        if path.exists() and any(existing_counts(path).values()) and not replace:
            raise ExportError(f"{path.name} is non-empty; use --replace only after backing up and stopping Docker")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agentmesh-import-", dir=directory) as temporary:
        stage = Path(temporary)
        originals = {}
        for name, target in targets.items():
            _stage_database(stage / target.name, payload["databases"][name], hub=name == "hub")
            if target.exists():
                backup = stage / (target.name + ".original")
                shutil.copy2(target, backup)
                originals[target] = backup
        installed = []
        try:
            for target in targets.values():
                safe_target(target, importing=True)
                os.replace(stage / target.name, target)
                installed.append(target)
        except BaseException:
            for target in reversed(installed):
                if target in originals:
                    os.replace(originals[target], target)
                else:
                    target.unlink()
            raise
    return {name: {table: len(content["rows"]) for table, content in source["tables"].items()}
            for name, source in payload["databases"].items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import an owner Worker export into stopped Docker SQLite data files")
    parser.add_argument("export", type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--replace", action="store_true", help="replace non-empty databases (back up and stop Docker first)")
    args = parser.parse_args(argv)
    try:
        counts = import_export(args.export, args.data_dir, replace=args.replace)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1
    for name, tables in counts.items():
        print(f"{DATABASES[name]}: imported {sum(tables.values())} rows across {len(tables)} tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
