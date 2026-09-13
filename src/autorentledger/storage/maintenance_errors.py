"""Focused SQLite persistence adapters."""

from __future__ import annotations


class MaintenanceStorageError(Exception):
    """Base error for transactional maintenance validation."""


class MaintenancePayerNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceAliasNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceAliasOwnerError(MaintenanceStorageError):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id


class MaintenanceRentAccountNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceAssociationNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceScheduleNotFoundError(MaintenanceStorageError):
    pass


class MaintenanceDateRangeError(MaintenanceStorageError):
    pass


class MaintenanceScheduleConflictError(MaintenanceStorageError):
    def __init__(self, schedule_id: int) -> None:
        self.schedule_id = schedule_id


class MaintenanceScheduleOutsideAccountRangeError(MaintenanceStorageError):
    pass
