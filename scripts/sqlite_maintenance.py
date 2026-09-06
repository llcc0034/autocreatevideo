"""SQLite backup and validation helpers for the operations scripts.

Django-backed SQLite schemas can contain constraints that call functions such
as ``REGEXP``. Bare ``sqlite3`` connections do not know those functions, so
maintenance connections register Django's exact SQLite function set before an
integrity check.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from django.db.backends.sqlite3._functions import register as register_django_functions


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    resolved = path.resolve(strict=read_only)
    if read_only:
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(str(resolved))
    register_django_functions(connection)
    return connection


def _assert_integrity(connection: sqlite3.Connection) -> None:
    results = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if results != ["ok"]:
        raise RuntimeError("SQLite integrity_check failed: " + " | ".join(results))


def backup_database(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source_path}")
    if destination_path.exists():
        raise FileExistsError(f"SQLite backup destination already exists: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = _connect(source_path, read_only=True)
    destination = _connect(destination_path, read_only=False)
    try:
        source.backup(destination)
        _assert_integrity(destination)
    finally:
        destination.close()
        source.close()


def validate_database(path: Path) -> None:
    connection = _connect(path, read_only=True)
    try:
        _assert_integrity(connection)
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("source", type=Path)
    backup_parser.add_argument("destination", type=Path)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "backup":
        backup_database(args.source, args.destination)
    else:
        validate_database(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
