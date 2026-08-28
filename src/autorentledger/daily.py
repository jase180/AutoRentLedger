"""One safe externally schedulable AutoRentLedger operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autorentledger.database import DatabaseBackupResult, backup_database
from autorentledger.operations import SyncResult
from autorentledger.storage.migrations import require_current_schema

SchemaChecker = Callable[[Path], None]
BackupOperation = Callable[..., DatabaseBackupResult]
SyncOperation = Callable[[], SyncResult]


class DailyOperationError(RuntimeError):
    """An expected daily-operation stage failed."""


class DailyBackupError(DailyOperationError):
    """The verified pre-sync backup could not be created."""


class DailySyncError(DailyOperationError):
    """Sync failed after a verified backup was created."""

    def __init__(self, backup_path: Path) -> None:
        super().__init__("Daily sync failed after the verified backup was created.")
        self.backup_path = backup_path


@dataclass(frozen=True)
class DailyOperationResult:
    backup_path: Path
    sync_result: SyncResult


def run_daily_operation(
    database_path: Path,
    backup_directory: Path,
    sync_operation: SyncOperation,
    *,
    now: datetime | None = None,
    schema_checker: SchemaChecker = require_current_schema,
    backup_operation: BackupOperation = backup_database,
) -> DailyOperationResult:
    """Verify readiness, create a verified backup, then run the existing sync."""
    schema_checker(database_path)
    try:
        backup = backup_operation(
            database_path,
            now=now,
            default_directory=backup_directory,
        )
    except Exception as error:
        raise DailyBackupError("Daily backup failed.") from error

    try:
        sync_result = sync_operation()
    except Exception as error:
        raise DailySyncError(backup.backup_path) from error

    return DailyOperationResult(backup.backup_path, sync_result)


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
