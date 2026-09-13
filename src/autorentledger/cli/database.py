"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
)
from autorentledger.database import (
    DatabaseHealthResult,
    DatabaseOperationError,
    backup_database,
    check_database,
    restore_database,
)
from autorentledger.storage.migrations import (
    DatabaseSchemaError,
    get_schema_status,
    upgrade_database,
)


def register_commands(subparsers) -> None:
    database = subparsers.add_parser("db", help="inspect or upgrade the database schema")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_status = database_commands.add_parser("status", help="show schema compatibility")
    database_status.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_upgrade = database_commands.add_parser("upgrade", help="upgrade schema explicitly")
    database_upgrade.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_check = database_commands.add_parser("check", help="verify database health")
    database_check.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_backup = database_commands.add_parser(
        "backup", help="create a verified SQLite backup"
    )
    database_backup.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    database_backup.add_argument("--output", type=Path, dest="output_path")
    database_restore = database_commands.add_parser(
        "restore", help="restore a verified SQLite backup"
    )
    database_restore.add_argument("backup_path", type=Path)
    database_restore.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

def run_database_status(database_path: Path) -> int:
    try:
        status = get_schema_status(database_path)
    except DatabaseSchemaError as error:
        print(error)
        return 1
    print(f"Database: {database_path}")
    print(f"Schema version: {status.schema_version}")
    if status.detected_legacy_version is not None:
        print(f"Detected legacy schema: version {status.detected_legacy_version}")
    print(f"Required version: {status.required_version}")
    print(f"Status: {status.state}")
    return 0

def run_database_upgrade(database_path: Path) -> int:
    try:
        result = upgrade_database(database_path)
    except DatabaseSchemaError as error:
        print(error)
        return 1
    if not result.changed:
        print(f"Database schema is already current at version {result.to_version}.")
        return 0
    print(
        f"Database schema upgraded from version {result.from_version} "
        f"to version {result.to_version}."
    )
    if result.backup_path is not None:
        print(f"Backup: {result.backup_path}")
    return 0

def run_database_check(database_path: Path) -> int:
    health = check_database(database_path)
    _print_database_health(health)
    return 0 if health.healthy else 1

def _print_database_health(health: DatabaseHealthResult) -> None:
    print("DATABASE HEALTH")
    print(f"Schema:        {_health_label(health.schema_ok)}")
    print(f"Integrity:     {_health_label(health.sqlite_integrity_ok)}")
    print(f"Foreign keys:  {_health_label(health.foreign_keys_ok)}")
    print(f"Ledger:        {_health_label(health.ledger_ok)}")
    for issue in health.issues:
        print(issue.message)
    print("Database healthy." if health.healthy else "Database unhealthy.")

def _health_label(ok: bool) -> str:
    return "OK" if ok else "FAILED"

def run_database_backup(database_path: Path, output_path: Path | None) -> int:
    try:
        result = backup_database(database_path, output_path=output_path)
    except (DatabaseOperationError, OSError, sqlite3.Error) as error:
        print(f"Database backup failed: {error}")
        return 1
    print(f"Backup created: {result.backup_path}")
    print("Backup verified healthy.")
    return 0

def run_database_restore(candidate_path: Path, database_path: Path) -> int:
    try:
        result = restore_database(candidate_path, database_path)
    except (DatabaseOperationError, OSError, sqlite3.Error) as error:
        print(f"Database restore failed: {error}")
        return 1
    print(f"Database restored from: {result.candidate_path}")
    if result.pre_restore_backup_path is not None:
        print(f"Pre-restore backup: {result.pre_restore_backup_path}")
    print("Restored database verified healthy.")
    return 0
