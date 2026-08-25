"""Narrow, explicit corrections for interpretation and configuration records."""

from __future__ import annotations

from datetime import date

from autorentledger.identity import normalize_alias
from autorentledger.storage import (
    MaintenanceAliasNotFoundError,
    MaintenanceAliasOwnerError,
    MaintenanceAssociationNotFoundError,
    MaintenanceDateRangeError,
    MaintenancePayerNotFoundError,
    MaintenanceRentAccountNotFoundError,
    MaintenanceScheduleConflictError,
    MaintenanceScheduleNotFoundError,
    MaintenanceScheduleOutsideAccountRangeError,
    PayerAliasRecord,
    PayerRecord,
    RentAccountPayerRecord,
    RentAccountRecord,
    RentScheduleRecord,
    SQLitePayerRepository,
    SQLiteRentalRepository,
    SQLiteRentScheduleRepository,
)


class MaintenanceValidationError(ValueError):
    """A correction value is invalid."""


class MaintenanceNotFoundError(ValueError):
    """A correction target or exact relationship does not exist."""


class MaintenanceConflictError(ValueError):
    """A correction conflicts with another explicit configuration record."""


def rename_payer(
    repository: SQLitePayerRepository, payer_id: int, display_name: str
) -> tuple[PayerRecord, PayerRecord]:
    cleaned_name = display_name.strip()
    if not cleaned_name:
        raise MaintenanceValidationError("Payer display name must not be empty.")
    try:
        return repository.rename_checked(payer_id, cleaned_name)
    except MaintenancePayerNotFoundError as error:
        raise MaintenanceNotFoundError(f"Payer {payer_id} does not exist.") from error


def remove_payer_alias(
    repository: SQLitePayerRepository, payer_id: int, alias: str
) -> PayerAliasRecord:
    normalized = normalize_alias(alias)
    if not normalized:
        raise MaintenanceValidationError("Alias must not be empty.")
    try:
        return repository.remove_alias_checked(payer_id, normalized)
    except MaintenancePayerNotFoundError as error:
        raise MaintenanceNotFoundError(f"Payer {payer_id} does not exist.") from error
    except MaintenanceAliasNotFoundError as error:
        raise MaintenanceNotFoundError(f'Alias "{alias}" does not exist.') from error
    except MaintenanceAliasOwnerError as error:
        raise MaintenanceConflictError(
            f'Alias "{alias}" belongs to payer {error.owner_id}, not payer {payer_id}.'
        ) from error


def rename_rent_account(
    repository: SQLiteRentalRepository, account_id: int, display_name: str
) -> tuple[RentAccountRecord, RentAccountRecord]:
    cleaned_name = display_name.strip()
    if not cleaned_name:
        raise MaintenanceValidationError("Rent account name must not be empty.")
    try:
        return repository.rename_rent_account_checked(account_id, cleaned_name)
    except MaintenanceRentAccountNotFoundError as error:
        raise MaintenanceNotFoundError(
            f"Rent account {account_id} does not exist."
        ) from error


def remove_rent_account_payer(
    repository: SQLiteRentalRepository, account_id: int, payer_id: int
) -> RentAccountPayerRecord:
    try:
        return repository.remove_payer_checked(account_id, payer_id)
    except MaintenanceRentAccountNotFoundError as error:
        raise MaintenanceNotFoundError(
            f"Rent account {account_id} does not exist."
        ) from error
    except MaintenancePayerNotFoundError as error:
        raise MaintenanceNotFoundError(f"Payer {payer_id} does not exist.") from error
    except MaintenanceAssociationNotFoundError as error:
        raise MaintenanceNotFoundError(
            f"Payer {payer_id} is not associated with rent account {account_id}."
        ) from error


def end_rent_account(
    repository: SQLiteRentalRepository, account_id: int, active_to: str
) -> tuple[RentAccountRecord, RentAccountRecord]:
    parsed_end = _parse_date(active_to, "active-to")
    try:
        return repository.end_rent_account_checked(account_id, parsed_end)
    except MaintenanceRentAccountNotFoundError as error:
        raise MaintenanceNotFoundError(
            f"Rent account {account_id} does not exist."
        ) from error
    except MaintenanceDateRangeError as error:
        raise MaintenanceValidationError(
            "Active-to date must not be before the rent account's active-from date."
        ) from error
    except MaintenanceScheduleConflictError as error:
        raise MaintenanceConflictError(
            f"Rent schedule {error.schedule_id} extends beyond {active_to}; "
            "end that schedule first."
        ) from error


def end_rent_schedule(
    repository: SQLiteRentScheduleRepository, schedule_id: int, active_to: str
) -> tuple[RentScheduleRecord, RentScheduleRecord]:
    parsed_end = _parse_date(active_to, "active-to")
    try:
        return repository.end_checked(schedule_id, parsed_end)
    except MaintenanceScheduleNotFoundError as error:
        raise MaintenanceNotFoundError(
            f"Rent schedule {schedule_id} does not exist."
        ) from error
    except MaintenanceRentAccountNotFoundError as error:
        raise MaintenanceNotFoundError("The schedule's rent account does not exist.") from error
    except MaintenanceDateRangeError as error:
        raise MaintenanceValidationError(
            "Active-to date must not be before the rent schedule's active-from date."
        ) from error
    except MaintenanceScheduleOutsideAccountRangeError as error:
        raise MaintenanceValidationError(
            "Schedule end date must remain within its rent account's active range."
        ) from error
    except MaintenanceScheduleConflictError as error:
        raise MaintenanceConflictError(
            f"Ending rent schedule {schedule_id} on {active_to} would overlap "
            f"rent schedule {error.schedule_id}."
        ) from error


def _parse_date(value: str, option_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise MaintenanceValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        ) from error
    if parsed.isoformat() != value:
        raise MaintenanceValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        )
    return parsed
