"""Verified SQLite backup and conservative staged restore operations."""

from __future__ import annotations

import gc
import os
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autorentledger.database.health import DatabaseHealthResult, check_database

HealthChecker = Callable[[Path], DatabaseHealthResult]
FileReplacer = Callable[[Path, Path], None]


class DatabaseOperationError(RuntimeError):
    """An expected backup or restore safety condition was not met."""


class DatabasePathConflictError(DatabaseOperationError):
    pass


class DatabaseUnhealthyError(DatabaseOperationError):
    pass


class DatabaseRestoreError(DatabaseOperationError):
    pass


@dataclass(frozen=True)
class DatabaseBackupResult:
    database_path: Path
    backup_path: Path
    health: DatabaseHealthResult


@dataclass(frozen=True)
class DatabaseRestoreResult:
    database_path: Path
    candidate_path: Path
    pre_restore_backup_path: Path | None
    health: DatabaseHealthResult


def backup_database(
    database_path: Path,
    *,
    output_path: Path | None = None,
    now: datetime | None = None,
    default_directory: Path = Path("backups"),
) -> DatabaseBackupResult:
    """Create and independently verify a coherent SQLite snapshot."""
    _require_healthy(database_path, "Source database")
    destination = _backup_destination(output_path, default_directory, now)
    if _same_path(database_path, destination):
        raise DatabasePathConflictError("Backup destination must differ from the source database.")
    if destination.exists():
        raise DatabasePathConflictError(f"Backup destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.touch(exist_ok=False)
    except FileExistsError as error:
        raise DatabasePathConflictError(
            f"Backup destination already exists: {destination}"
        ) from error
    try:
        _sqlite_snapshot(database_path, destination, allow_empty_destination=True)
        backup_health = _require_healthy(destination, "Created backup")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return DatabaseBackupResult(database_path, destination, backup_health)


def restore_database(
    candidate_path: Path,
    database_path: Path,
    *,
    now: datetime | None = None,
    backup_directory: Path = Path("backups"),
    health_checker: HealthChecker = check_database,
    replace_file: FileReplacer = os.replace,
) -> DatabaseRestoreResult:
    """Validate, stage, atomically replace, and verify a database with rollback."""
    if _same_path(candidate_path, database_path):
        raise DatabasePathConflictError("Restore candidate must differ from the active database.")
    _require_healthy(candidate_path, "Restore candidate", health_checker)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    staged_path = _temporary_database_path(database_path, "restore")
    rollback_stage: Path | None = None
    pre_restore_backup: Path | None = None
    replaced = False
    try:
        _sqlite_snapshot(candidate_path, staged_path, allow_empty_destination=True)
        _require_healthy(staged_path, "Staged restore", health_checker)

        if database_path.exists():
            safety_path = _pre_restore_destination(backup_directory, now)
            pre_restore_backup = backup_database(
                database_path,
                output_path=safety_path,
            ).backup_path

        _replace_atomically(staged_path, database_path, replace_file)
        replaced = True
        _remove_sqlite_sidecars(database_path)
        final_health = _require_healthy(
            database_path, "Restored database", health_checker
        )
        return DatabaseRestoreResult(
            database_path,
            candidate_path,
            pre_restore_backup,
            final_health,
        )
    except Exception as error:
        if replaced:
            if pre_restore_backup is not None:
                try:
                    rollback_stage = _temporary_database_path(database_path, "rollback")
                    _sqlite_snapshot(
                        pre_restore_backup,
                        rollback_stage,
                        allow_empty_destination=True,
                    )
                    _require_healthy(rollback_stage, "Rollback stage")
                    _replace_atomically(rollback_stage, database_path, replace_file)
                    rollback_stage = None
                    _remove_sqlite_sidecars(database_path)
                    _require_healthy(database_path, "Rolled-back database")
                except Exception as rollback_error:
                    raise DatabaseRestoreError(
                        "Restore failed and automatic rollback could not be verified. "
                        f"Recovery backup remains at: {pre_restore_backup}"
                    ) from rollback_error
                raise DatabaseRestoreError(
                    "Restore failed final validation; the original database was restored from "
                    f"the verified safety backup: {pre_restore_backup}"
                ) from error
            failed_path = _failed_restore_destination(backup_directory, now)
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            _replace_atomically(database_path, failed_path, replace_file)
            raise DatabaseRestoreError(
                "Restore failed final validation; no prior active database existed. "
                f"The failed staged database was preserved at: {failed_path}"
            ) from error
        if isinstance(error, DatabaseOperationError):
            raise
        raise DatabaseRestoreError(f"Restore failed before replacement: {type(error).__name__}.") from error
    finally:
        staged_path.unlink(missing_ok=True)
        if rollback_stage is not None:
            rollback_stage.unlink(missing_ok=True)


def _require_healthy(
    database_path: Path,
    label: str,
    health_checker: HealthChecker = check_database,
) -> DatabaseHealthResult:
    health = health_checker(database_path)
    if health.healthy:
        return health
    detail = health.issues[0].message if health.issues else "unknown health failure"
    raise DatabaseUnhealthyError(f"{label} is unhealthy: {detail}")


def _sqlite_snapshot(
    source_path: Path,
    destination_path: Path,
    *,
    allow_empty_destination: bool = False,
) -> None:
    if destination_path.exists() and not (
        allow_empty_destination and destination_path.stat().st_size == 0
    ):
        raise DatabasePathConflictError(
            f"Snapshot destination already exists: {destination_path}"
        )
    source_uri = source_path.resolve().as_uri() + "?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True)) as source,
        closing(sqlite3.connect(destination_path)) as destination,
    ):
        source.execute("PRAGMA query_only = ON")
        source.backup(destination)


def _backup_destination(
    output_path: Path | None,
    default_directory: Path,
    now: datetime | None,
) -> Path:
    if output_path is not None:
        return output_path
    timestamp = _timestamp(now)
    base = default_directory / f"autorentledger-{timestamp}.db"
    return _available_path(base)


def _pre_restore_destination(directory: Path, now: datetime | None) -> Path:
    base = directory / f"autorentledger-pre-restore-{_timestamp(now)}.db"
    return _available_path(base)


def _failed_restore_destination(directory: Path, now: datetime | None) -> Path:
    base = directory / f"autorentledger-failed-restore-{_timestamp(now)}.db"
    return _available_path(base)


def _available_path(base: Path) -> Path:
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
        suffix += 1
    return candidate


def _timestamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _temporary_database_path(database_path: Path, purpose: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{database_path.name}.{purpose}-",
        suffix=".db",
        dir=database_path.parent,
    )
    os.close(descriptor)
    return Path(name)


def _same_path(first: Path, second: Path) -> bool:
    return first.resolve(strict=False) == second.resolve(strict=False)


def _remove_sqlite_sidecars(database_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)


def _replace_atomically(source: Path, destination: Path, replace_file: FileReplacer) -> None:
    try:
        replace_file(source, destination)
    except PermissionError:
        # sqlite3 connections participate in cyclic GC on some Python/Windows builds.
        # Collect unreachable, already-out-of-scope handles once before reporting a real lock.
        gc.collect()
        replace_file(source, destination)
