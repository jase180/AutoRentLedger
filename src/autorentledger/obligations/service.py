"""Validation and creation of explicit monthly rent obligations."""

import calendar
import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from autorentledger.storage import (
    RentObligationRecord,
    SQLiteObligationRepository,
    SQLiteRentalRepository,
)

_PERIOD_PATTERN = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_CURRENCY_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ObligationValidationError(ValueError):
    """An obligation input or active-range relationship is invalid."""


class ObligationAccountNotFoundError(ValueError):
    """The requested rent account does not exist."""


class DuplicateObligationError(ValueError):
    """The rent account already has an obligation for the period."""


@dataclass(frozen=True)
class MonthlyPeriod:
    value: str
    first_day: date
    last_day: date


def parse_monthly_period(value: str) -> MonthlyPeriod:
    """Parse exactly YYYY-MM and expose the month's inclusive date boundaries."""
    if _PERIOD_PATTERN.fullmatch(value) is None:
        raise ObligationValidationError(
            f"Invalid period {value!r}; expected canonical YYYY-MM."
        )
    year, month = (int(part) for part in value.split("-"))
    try:
        first_day = date(year, month, 1)
    except ValueError as error:
        raise ObligationValidationError(
            f"Invalid period {value!r}; expected canonical YYYY-MM."
        ) from error
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return MonthlyPeriod(value, first_day, last_day)


def parse_currency_cents(value: str) -> int:
    """Convert plain decimal currency text to exact positive integer cents."""
    cleaned = value.strip()
    if _CURRENCY_PATTERN.fullmatch(cleaned) is None:
        raise ObligationValidationError(
            f"Invalid amount {value!r}; expected a positive decimal amount."
        )
    whole, separator, fraction = cleaned.partition(".")
    amount_cents = int(whole) * 100
    if separator:
        amount_cents += int(fraction.ljust(2, "0"))
    if amount_cents <= 0:
        raise ObligationValidationError("Amount must be greater than zero.")
    return amount_cents


def parse_iso_date(value: str) -> date:
    """Parse exactly YYYY-MM-DD as a calendar date."""
    if _DATE_PATTERN.fullmatch(value) is None:
        raise ObligationValidationError(
            f"Invalid due date {value!r}; expected YYYY-MM-DD."
        )
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ObligationValidationError(
            f"Invalid due date {value!r}; expected YYYY-MM-DD."
        ) from error


def create_obligation(
    obligations: SQLiteObligationRepository,
    rentals: SQLiteRentalRepository,
    rent_account_id: int,
    period: str,
    amount: str,
    due_date: str,
) -> RentObligationRecord:
    account = rentals.get_rent_account(rent_account_id)
    if account is None:
        raise ObligationAccountNotFoundError(f"Rent account {rent_account_id} does not exist.")

    parsed_period = parse_monthly_period(period)
    amount_cents = parse_currency_cents(amount)
    parsed_due_date = parse_iso_date(due_date)
    _validate_active_range(account.active_from, account.active_to, parsed_period)

    if obligations.get_for_account_period(rent_account_id, parsed_period.value) is not None:
        raise DuplicateObligationError(
            f"Obligation already exists for rent account {rent_account_id} "
            f"and period {parsed_period.value}."
        )
    try:
        return obligations.create(
            rent_account_id,
            parsed_period.value,
            amount_cents,
            parsed_due_date,
        )
    except sqlite3.IntegrityError as error:
        if obligations.get_for_account_period(rent_account_id, parsed_period.value) is not None:
            raise DuplicateObligationError(
                f"Obligation already exists for rent account {rent_account_id} "
                f"and period {parsed_period.value}."
            ) from error
        raise


def _validate_active_range(
    active_from: str | None,
    active_to: str | None,
    period: MonthlyPeriod,
) -> None:
    start = date.fromisoformat(active_from) if active_from else None
    end = date.fromisoformat(active_to) if active_to else None
    if start is not None and period.last_day < start:
        raise ObligationValidationError(
            f"Period {period.value} is entirely before the rent account became active."
        )
    if end is not None and period.first_day > end:
        raise ObligationValidationError(
            f"Period {period.value} is entirely after the rent account ended."
        )
