"""Business-facing recurring-rent changes over effective-dated schedules."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

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
    TenancyEndExistingObligationStorageError,
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


@dataclass(frozen=True)
class TenancyEndRequest:
    rent_account_id: int
    active_to: str
    final_month_rent: str | None = None
    final_month_due: str | None = None
    no_final_month_rent: bool = False


@dataclass(frozen=True)
class TenancyEndPreview:
    account_display_name: str
    actual_active_to: date
    final_month_period: str
    is_partial_month: bool
    schedule_active_to: date
    ended_schedule_ids: tuple[int, ...]
    final_month_amount_cents: int | None
    final_month_due: date | None
    no_final_month_rent: bool


@dataclass(frozen=True)
class _TenancyEndPlan:
    active_to: date
    final_month_period: str
    is_partial_month: bool
    schedule_active_to: date
    final_month_amount_cents: int | None
    final_month_due: date | None


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


def preview_tenancy_end(
    repository: SQLiteRentScheduleRepository,
    request: TenancyEndRequest,
) -> TenancyEndPreview:
    """Build a read-only, checked tenancy-end plan."""
    plan = _validate_tenancy_end(request)
    try:
        state = repository.preview_tenancy_end_checked(
            request.rent_account_id,
            plan.active_to,
            schedule_active_to=plan.schedule_active_to,
            final_month_override=(
                plan.final_month_amount_cents is not None
                or request.no_final_month_rent
            ),
        )
    except (
        MaintenanceRentAccountNotFoundError,
        MaintenanceDateRangeError,
        TenancyEndFutureScheduleStorageError,
        TenancyEndExistingObligationStorageError,
    ) as error:
        _raise_tenancy_end_error(error, request.rent_account_id)
    return TenancyEndPreview(
        account_display_name=state.account.display_name,
        actual_active_to=plan.active_to,
        final_month_period=plan.final_month_period,
        is_partial_month=plan.is_partial_month,
        schedule_active_to=plan.schedule_active_to,
        ended_schedule_ids=state.ended_schedule_ids,
        final_month_amount_cents=plan.final_month_amount_cents,
        final_month_due=plan.final_month_due,
        no_final_month_rent=request.no_final_month_rent,
    )


def end_tenancy(
    repository: SQLiteRentScheduleRepository,
    rent_account_id: int,
    active_to: str,
    *,
    final_month_rent: str | None = None,
    final_month_due: str | None = None,
    no_final_month_rent: bool = False,
) -> TenancyEndStorageResult:
    """Apply a checked tenancy end without deleting ledger history."""
    request = TenancyEndRequest(
        rent_account_id=rent_account_id,
        active_to=active_to,
        final_month_rent=final_month_rent,
        final_month_due=final_month_due,
        no_final_month_rent=no_final_month_rent,
    )
    plan = _validate_tenancy_end(request)
    try:
        return repository.end_tenancy_checked(
            rent_account_id,
            plan.active_to,
            schedule_active_to=plan.schedule_active_to,
            final_month_amount_cents=plan.final_month_amount_cents,
            final_month_due=plan.final_month_due,
        )
    except MaintenanceRentAccountNotFoundError as error:
        _raise_tenancy_end_error(error, rent_account_id)
    except (
        MaintenanceDateRangeError,
        TenancyEndFutureScheduleStorageError,
        TenancyEndExistingObligationStorageError,
    ) as error:
        _raise_tenancy_end_error(error, rent_account_id)
    raise AssertionError("unreachable")


def _validate_tenancy_end(request: TenancyEndRequest) -> _TenancyEndPlan:
    active_to = _parse_date(request.active_to, "active-to")
    is_partial = active_to.day != monthrange(active_to.year, active_to.month)[1]
    has_override = request.final_month_rent is not None
    if request.final_month_due is not None and not has_override:
        raise RentOperationValidationError(
            "--final-month-due requires --final-month-rent."
        )
    if is_partial and has_override == request.no_final_month_rent:
        raise RentOperationValidationError(
            "A mid-month tenancy end requires exactly one of "
            "--final-month-rent or --no-final-month-rent."
        )
    if not is_partial and (has_override or request.no_final_month_rent):
        raise RentOperationValidationError(
            "Final-month override options are only supported for a partial final month."
        )

    amount_cents = None
    due = None
    if has_override:
        try:
            amount_cents = parse_currency_cents(request.final_month_rent or "")
        except ObligationValidationError as error:
            raise RentOperationValidationError(str(error)) from error
        due = (
            _parse_date(request.final_month_due, "final-month-due")
            if request.final_month_due is not None
            else active_to.replace(day=1)
        )
        if (due.year, due.month) != (active_to.year, active_to.month):
            raise RentOperationValidationError(
                "Final-month due date must be inside the final tenancy month."
            )

    schedule_active_to = (
        active_to.replace(day=1) - timedelta(days=1) if is_partial else active_to
    )
    return _TenancyEndPlan(
        active_to=active_to,
        final_month_period=active_to.strftime("%Y-%m"),
        is_partial_month=is_partial,
        schedule_active_to=schedule_active_to,
        final_month_amount_cents=amount_cents,
        final_month_due=due,
    )


def _raise_tenancy_end_error(error: Exception, rent_account_id: int) -> None:
    if isinstance(error, MaintenanceRentAccountNotFoundError):
        raise RentOperationNotFoundError(
            f"Rent account {rent_account_id} does not exist."
        ) from error
    if isinstance(error, MaintenanceDateRangeError):
        raise RentOperationValidationError(
            "Active-to date must not be before the account's active-from date "
            "or extend an already ended tenancy."
        ) from error
    if isinstance(error, TenancyEndFutureScheduleStorageError):
        raise RentOperationConflictError(
            "The account has a schedule beginning after the safe recurring-rent "
            "cutoff; review schedule history before ending the tenancy."
        ) from error
    if isinstance(error, TenancyEndExistingObligationStorageError):
        if error.has_allocations:
            raise RentOperationConflictError(
                f"Final-month obligation for {error.period} already has allocations. "
                "Remove or reconcile allocations before changing the final-month amount."
            ) from error
        raise RentOperationConflictError(
            f"Final-month obligation for {error.period} already exists and was not "
            "rewritten. Use the advanced repair path before ending the tenancy."
        ) from error
    raise error


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
    "TenancyEndPreview",
    "TenancyEndRequest",
    "change_recurring_rent",
    "end_tenancy",
    "preview_tenancy_end",
]
