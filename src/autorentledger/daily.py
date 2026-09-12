"""One safe externally schedulable AutoRentLedger operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from autorentledger.database import DatabaseBackupResult, backup_database
from autorentledger.operations import SyncResult, refresh_sync_projections
from autorentledger.retention import (
    BackupRetentionResult,
    daily_backup_destination,
    prune_daily_backups,
)
from autorentledger.schedules import ObligationGenerationPlan, generate_obligations
from autorentledger.storage import (
    SQLiteReconciliationRepository,
    SQLiteRentScheduleRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.storage.migrations import require_current_schema

SchemaChecker = Callable[[Path], None]
BackupOperation = Callable[..., DatabaseBackupResult]
SyncOperation = Callable[[], SyncResult]
RetentionOperation = Callable[[Path, int, Path], BackupRetentionResult]
ObligationOperation = Callable[[str], ObligationGenerationPlan]
ProjectionOperation = Callable[[SyncResult], SyncResult]


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


class DailyObligationError(DailyOperationError):
    """Current-month obligation generation failed after sync."""

    def __init__(self, backup_path: Path, period: str) -> None:
        super().__init__(f"Daily obligation generation failed for {period}.")
        self.backup_path = backup_path
        self.period = period


class DailyProjectionError(DailyOperationError):
    """Derived attention output could not be refreshed after generation."""

    def __init__(self, backup_path: Path, period: str) -> None:
        super().__init__(f"Daily attention refresh failed for {period}.")
        self.backup_path = backup_path
        self.period = period


@dataclass(frozen=True)
class DailyOperationResult:
    backup_path: Path
    sync_result: SyncResult
    retention: BackupRetentionResult
    obligation_generation: ObligationGenerationPlan | None = None


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
    obligation_operation: ObligationOperation | None = None,
    projection_operation: ProjectionOperation | None = None,
    today: date | None = None,
    skip_obligations: bool = False,
) -> DailyOperationResult:
    """Back up, sync, ensure this month's obligations, refresh, then retain backups."""
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

    obligation_generation = None
    if not skip_obligations:
        effective_date = today or date.today()  # noqa: DTZ011 - host-local calendar month
        target_period = effective_date.strftime("%Y-%m")
        generate = obligation_operation or (
            lambda period: generate_obligations(
                SQLiteRentScheduleRepository(database_path), period
            )
        )
        try:
            obligation_generation = generate(target_period)
        except Exception as error:
            raise DailyObligationError(backup.backup_path, target_period) from error

        refresh = projection_operation or (
            lambda result: refresh_sync_projections(
                result,
                SQLiteReconciliationRepository(database_path),
                SQLiteReviewRepository(database_path),
                SQLiteSuggestionRepository(database_path),
            )
        )
        try:
            sync_result = refresh(sync_result)
        except Exception as error:
            raise DailyProjectionError(backup.backup_path, target_period) from error

    try:
        retention = retention_operation(
            backup_directory,
            keep_backups,
            backup.backup_path,
        )
    except Exception as error:
        raise DailyRetentionError(backup.backup_path) from error

    return DailyOperationResult(
        backup.backup_path,
        sync_result,
        retention,
        obligation_generation,
    )


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
