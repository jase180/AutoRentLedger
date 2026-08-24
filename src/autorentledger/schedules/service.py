"""Effective-dated rent schedules and explicit obligation generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from autorentledger.obligations import (
    MonthlyPeriod,
    ObligationValidationError,
    parse_currency_cents,
    parse_monthly_period,
)
from autorentledger.storage import (
    ObligationGenerationSourceRecord,
    RentScheduleAccountNotFoundError,
    RentScheduleOutsideAccountRangeError,
    RentScheduleOverlapStorageError,
    RentScheduleRecord,
    SQLiteRentScheduleRepository,
)


class RentScheduleValidationError(ValueError):
    """A schedule input or effective-range relationship is invalid."""


class RentScheduleAccountMissingError(ValueError):
    """The requested rent account does not exist."""


class RentScheduleOverlapError(ValueError):
    """Two effective ranges overlap for one rent account."""


class ObligationGenerationInvariantError(RuntimeError):
    """Stored schedules are ambiguous for monthly generation."""


class GenerationAction(StrEnum):
    CREATE = "CREATE"
    SKIP = "SKIP"


@dataclass(frozen=True)
class ObligationGenerationItem:
    action: GenerationAction
    schedule_id: int
    rent_account_id: int
    unit_label: str
    account_display_name: str
    period: str
    amount_cents: int
    due_date: date
    reason: str | None


@dataclass(frozen=True)
class ObligationGenerationPlan:
    period: str
    items: tuple[ObligationGenerationItem, ...]

    @property
    def create_count(self) -> int:
        return sum(item.action is GenerationAction.CREATE for item in self.items)

    @property
    def skip_count(self) -> int:
        return sum(item.action is GenerationAction.SKIP for item in self.items)


def create_rent_schedule(
    repository: SQLiteRentScheduleRepository,
    rent_account_id: int,
    amount: str,
    due_day: int,
    active_from: str,
    active_to: str | None = None,
) -> RentScheduleRecord:
    try:
        amount_cents = parse_currency_cents(amount)
    except ObligationValidationError as error:
        raise RentScheduleValidationError(str(error)) from error
    if not 1 <= due_day <= 28:
        raise RentScheduleValidationError("Due day must be between 1 and 28.")
    start = _parse_date(active_from, "active-from")
    end = _parse_date(active_to, "active-to") if active_to is not None else None
    if end is not None and end < start:
        raise RentScheduleValidationError(
            "Active-to date must not be before active-from date."
        )

    try:
        return repository.create_checked(
            rent_account_id,
            amount_cents,
            due_day,
            start,
            end,
        )
    except RentScheduleAccountNotFoundError as error:
        raise RentScheduleAccountMissingError(
            f"Rent account {rent_account_id} does not exist."
        ) from error
    except RentScheduleOutsideAccountRangeError as error:
        raise RentScheduleValidationError(
            f"Schedule must be contained within rent account {rent_account_id}'s active range."
        ) from error
    except RentScheduleOverlapStorageError as error:
        raise RentScheduleOverlapError(
            f"Rent schedule overlaps an existing schedule for rent account {rent_account_id}."
        ) from error


def plan_obligation_generation(
    repository: SQLiteRentScheduleRepository,
    period: str,
) -> ObligationGenerationPlan:
    parsed_period = parse_monthly_period(period)
    sources = repository.list_generation_sources(
        parsed_period.value,
        parsed_period.first_day.isoformat(),
        parsed_period.last_day.isoformat(),
    )
    return _build_plan(parsed_period, sources)


def generate_obligations(
    repository: SQLiteRentScheduleRepository,
    period: str,
) -> ObligationGenerationPlan:
    parsed_period = parse_monthly_period(period)
    with repository.generation_transaction() as transaction:
        sources = transaction.list_sources(
            parsed_period.value,
            parsed_period.first_day.isoformat(),
            parsed_period.last_day.isoformat(),
        )
        plan = _build_plan(parsed_period, sources)
        for item in plan.items:
            if item.action is GenerationAction.CREATE:
                transaction.insert_obligation(
                    item.rent_account_id,
                    item.period,
                    item.amount_cents,
                    item.due_date,
                )
    return plan


def _build_plan(
    period: MonthlyPeriod,
    sources: list[ObligationGenerationSourceRecord],
) -> ObligationGenerationPlan:
    seen_accounts: set[int] = set()
    items: list[ObligationGenerationItem] = []
    for source in sources:
        if source.rent_account_id in seen_accounts:
            raise ObligationGenerationInvariantError(
                "Obligation generation is ambiguous for rent account "
                f"{source.rent_account_id} and period {period.value}: "
                "multiple schedules apply."
            )
        seen_accounts.add(source.rent_account_id)
        exists = source.existing_obligation_id is not None
        items.append(
            ObligationGenerationItem(
                action=GenerationAction.SKIP if exists else GenerationAction.CREATE,
                schedule_id=source.schedule_id,
                rent_account_id=source.rent_account_id,
                unit_label=source.unit_label,
                account_display_name=source.account_display_name,
                period=period.value,
                amount_cents=source.amount_cents,
                due_date=period.first_day.replace(day=source.due_day),
                reason="obligation already exists" if exists else None,
            )
        )
    return ObligationGenerationPlan(period.value, tuple(items))


def _parse_date(value: str, option_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise RentScheduleValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        ) from error
    if parsed.isoformat() != value:
        raise RentScheduleValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        )
    return parsed
