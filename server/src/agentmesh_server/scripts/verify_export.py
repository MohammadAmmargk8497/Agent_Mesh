"""Read-only row-count verification of both imported SQLite files."""

import argparse
import sqlite3
import sys
from pathlib import Path

from ._sqlite_export import (
    DATABASES,
    existing_counts,
    load_export,
    safe_directory,
    safe_target,
)


def verify_export(export_path, data_dir, *, output=None):
    output = output or sys.stdout
    payload = load_export(export_path)
    directory = safe_directory(data_dir)
    matches = True
    for name, filename in DATABASES.items():
        path = directory / filename
        safe_target(path)
        actual = existing_counts(path)
        expected = {table: len(content["rows"])
                    for table, content in payload["databases"][name]["tables"].items()}
        for table in sorted(set(actual) | set(expected)):
            equal = table in actual and table in expected and actual[table] == expected[table]
            matches = matches and equal
            print(f"{name}.{table}: export={expected.get(table, 'missing')} database={actual.get(table, 'missing')} "
                  f"{'OK' if equal else 'MISMATCH'}", file=output)
    print("PASS" if matches else "FAIL", file=output)
    return matches


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare every table's row count in an export and the imported Docker databases")
    parser.add_argument("export", type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return 0 if verify_export(args.export, args.data_dir) else 1
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
