"""One safe externally schedulable AutoRentLedger operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autorentledger.database import DatabaseBackupResult, backup_database
from autorentledger.operations import SyncResult
from autorentledger.retention import (
    BackupRetentionResult,
    daily_backup_destination,
    prune_daily_backups,
)
from autorentledger.storage.migrations import require_current_schema

SchemaChecker = Callable[[Path], None]
BackupOperation = Callable[..., DatabaseBackupResult]
SyncOperation = Callable[[], SyncResult]
RetentionOperation = Callable[[Path, int, Path], BackupRetentionResult]


class DailyOperationError(RuntimeError):
    """An expected daily-operation stage failed."""


class DailyBackupError(DailyOperationError):
    """The verified pre-sync backup could not be created."""


class GmailAccessError(RuntimeError):
    """Gmail authentication or message access failed at an operational boundary."""


class DailySyncError(DailyOperationError):
    """Sync failed after a verified backup was created."""

    def __init__(self, backup_path: Path) -> None:
        super().__init__("Daily sync failed after the verified backup was created.")
        self.backup_path = backup_path


class DailyGmailAccessError(DailySyncError):
    """Gmail authentication or message access failed after backup."""


class DailyRetentionError(DailyOperationError):
    """Retention failed after backup and sync both completed."""

    def __init__(self, backup_path: Path) -> None:
        super().__init__("Daily retention failed after backup and sync completed.")
        self.backup_path = backup_path


@dataclass(frozen=True)
class DailyOperationResult:
    backup_path: Path
    sync_result: SyncResult
    retention: BackupRetentionResult


def run_daily_operation(
    database_path: Path,
    backup_directory: Path,
    sync_operation: SyncOperation,
    *,
    keep_backups: int = 30,
    now: datetime | None = None,
    schema_checker: SchemaChecker = require_current_schema,
    backup_operation: BackupOperation = backup_database,
    retention_operation: RetentionOperation = prune_daily_backups,
) -> DailyOperationResult:
    """Verify, back up, sync, and only then prune eligible daily backups."""
    if keep_backups <= 0:
        raise ValueError("Backup retention count must be a positive integer.")
    schema_checker(database_path)
    backup_path = daily_backup_destination(backup_directory, now=now)
    try:
        backup = backup_operation(
            database_path,
            output_path=backup_path,
        )
    except Exception as error:
        raise DailyBackupError("Daily backup failed.") from error

    try:
        sync_result = sync_operation()
    except GmailAccessError as error:
        raise DailyGmailAccessError(backup.backup_path) from error
    except Exception as error:
        raise DailySyncError(backup.backup_path) from error

    try:
        retention = retention_operation(
            backup_directory,
            keep_backups,
            backup.backup_path,
        )
    except Exception as error:
        raise DailyRetentionError(backup.backup_path) from error

    return DailyOperationResult(backup.backup_path, sync_result, retention)


def daily_needs_attention(result: SyncResult) -> bool:
    """Return whether canonical review or suggestion output needs owner attention."""
    review = result.review
    return any(
        (
            review.unresolved_payers,
            review.unallocated_payments,
            review.partial_obligations,
            review.unpaid_obligations,
            review.unparsed_emails,
            len(result.actionable_suggestions),
        )
    )
