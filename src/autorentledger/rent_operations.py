"""Business-facing recurring-rent changes over effective-dated schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from autorentledger.obligations import ObligationValidationError, parse_currency_cents
from autorentledger.storage import (
    MaintenanceDateRangeError,
    MaintenanceRentAccountNotFoundError,
    RentChangeExistingObligationStorageError,
    RentChangeScheduleStorageError,
    RentScheduleAccountNotFoundError,
    RentScheduleOutsideAccountRangeError,
    RentScheduleOverlapStorageError,
    RentScheduleRecord,
    SQLiteRentScheduleRepository,
    TenancyEndFutureScheduleStorageError,
    TenancyEndStorageResult,
)


class RentOperationValidationError(ValueError):
    """A high-level recurring-rent request is invalid."""


class RentOperationNotFoundError(ValueError):
    """The requested rent account or current schedule does not exist."""


class RentOperationConflictError(ValueError):
    """Existing durable state makes the requested operation unsafe."""


@dataclass(frozen=True)
class RentChangeResult:
    previous_schedule: RentScheduleRecord
    new_schedule: RentScheduleRecord


def change_recurring_rent(
    repository: SQLiteRentScheduleRepository,
    rent_account_id: int,
    amount: str,
    effective: str,
) -> RentChangeResult:
    """Create effective-dated schedule history without rewriting prior charges."""
    try:
        amount_cents = parse_currency_cents(amount)
    except ObligationValidationError as error:
        raise RentOperationValidationError(str(error)) from error
    effective_on = _parse_date(effective, "effective")
    if effective_on.day != 1:
        raise RentOperationValidationError(
            "Effective date must be the first day of a month."
        )
    try:
        result = repository.change_rent_checked(
            rent_account_id, amount_cents, effective_on
        )
    except RentScheduleAccountNotFoundError as error:
        raise RentOperationNotFoundError(
            f"Rent account {rent_account_id} does not exist."
        ) from error
    except RentScheduleOutsideAccountRangeError as error:
        raise RentOperationValidationError(
            "Effective date must be within the rent account's active range."
        ) from error
    except RentChangeScheduleStorageError as error:
        raise RentOperationConflictError(
            f"Rent account {rent_account_id} does not have exactly one current schedule "
            f"to change before {effective}."
        ) from error
    except RentChangeExistingObligationStorageError as error:
        raise RentOperationConflictError(
            f"Rent account {rent_account_id} already has an obligation for "
            f"{effective_on:%Y-%m}; it was not rewritten."
        ) from error
    except RentScheduleOverlapStorageError as error:
        raise RentOperationConflictError(
            f"Rent account {rent_account_id} has conflicting future schedule history."
        ) from error
    return RentChangeResult(result.previous_schedule, result.new_schedule)


def end_tenancy(
    repository: SQLiteRentScheduleRepository,
    rent_account_id: int,
    active_to: str,
) -> TenancyEndStorageResult:
    """End future account/schedule applicability without deleting ledger history."""
    parsed_end = _parse_date(active_to, "active-to")
    try:
        return repository.end_tenancy_checked(rent_account_id, parsed_end)
    except MaintenanceRentAccountNotFoundError as error:
        raise RentOperationNotFoundError(
            f"Rent account {rent_account_id} does not exist."
        ) from error
    except MaintenanceDateRangeError as error:
        raise RentOperationValidationError(
            "Active-to date must not be before the account's active-from date "
            "or extend an already ended tenancy."
        ) from error
    except TenancyEndFutureScheduleStorageError as error:
        raise RentOperationConflictError(
            "The account has a schedule beginning after the requested tenancy end; "
            "review schedule history before ending the tenancy."
        ) from error


def _parse_date(value: str, option_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise RentOperationValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        ) from error
    if parsed.isoformat() != value:
        raise RentOperationValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        )
    return parsed


__all__ = [
    "RentChangeResult",
    "RentOperationConflictError",
    "RentOperationNotFoundError",
    "RentOperationValidationError",
    "change_recurring_rent",
    "end_tenancy",
]
