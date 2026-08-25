"""Database-wide health, backup, and restore operations."""

from autorentledger.database.backup import (
    DatabaseBackupResult,
    DatabaseOperationError,
    DatabasePathConflictError,
    DatabaseRestoreError,
    DatabaseRestoreResult,
    DatabaseUnhealthyError,
    backup_database,
    restore_database,
)
from autorentledger.database.health import (
    DatabaseHealthCategory,
    DatabaseHealthIssue,
    DatabaseHealthResult,
    check_database,
)

__all__ = [
    "DatabaseBackupResult",
    "DatabaseHealthCategory",
    "DatabaseHealthIssue",
    "DatabaseHealthResult",
    "DatabaseOperationError",
    "DatabasePathConflictError",
    "DatabaseRestoreError",
    "DatabaseRestoreResult",
    "DatabaseUnhealthyError",
    "backup_database",
    "check_database",
    "restore_database",
]
