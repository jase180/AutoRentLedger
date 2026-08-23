"""Validation and orchestration for units and rent-account membership."""

import sqlite3
from datetime import date

from autorentledger.storage import (
    RentAccountPayerRecord,
    RentAccountRecord,
    SQLitePayerRepository,
    SQLiteRentalRepository,
    UnitRecord,
)


class RentalValidationError(ValueError):
    """A supplied rental-domain value is invalid."""


class RentalEntityNotFoundError(ValueError):
    """A referenced unit, account, or payer does not exist."""


class DuplicateUnitError(ValueError):
    """A unit already uses the requested label."""


class DuplicateAssociationError(ValueError):
    """The payer is already associated with the rent account."""


def create_unit(repository: SQLiteRentalRepository, label: str) -> UnitRecord:
    cleaned_label = label.strip()
    if not cleaned_label:
        raise RentalValidationError("Unit label must not be empty.")
    try:
        return repository.create_unit(cleaned_label)
    except sqlite3.IntegrityError as error:
        raise DuplicateUnitError(f'Unit label "{cleaned_label}" already exists.') from error


def create_rent_account(
    repository: SQLiteRentalRepository,
    unit_id: int,
    display_name: str,
    active_from: str | None = None,
    active_to: str | None = None,
) -> RentAccountRecord:
    cleaned_name = display_name.strip()
    if not cleaned_name:
        raise RentalValidationError("Rent account name must not be empty.")
    if repository.get_unit(unit_id) is None:
        raise RentalEntityNotFoundError(f"Unit {unit_id} does not exist.")

    start = _parse_date(active_from, "active-from")
    end = _parse_date(active_to, "active-to")
    if start is not None and end is not None and end < start:
        raise RentalValidationError("Active-to date must not be before active-from date.")
    return repository.create_rent_account(unit_id, cleaned_name, start, end)


def associate_payer(
    rentals: SQLiteRentalRepository,
    payers: SQLitePayerRepository,
    account_id: int,
    payer_id: int,
) -> RentAccountPayerRecord:
    if rentals.get_rent_account(account_id) is None:
        raise RentalEntityNotFoundError(f"Rent account {account_id} does not exist.")
    if payers.get_payer(payer_id) is None:
        raise RentalEntityNotFoundError(f"Payer {payer_id} does not exist.")
    if rentals.has_payer(account_id, payer_id):
        raise DuplicateAssociationError(
            f"Payer {payer_id} is already associated with rent account {account_id}."
        )
    try:
        return rentals.add_payer(account_id, payer_id)
    except sqlite3.IntegrityError as error:
        if rentals.has_payer(account_id, payer_id):
            raise DuplicateAssociationError(
                f"Payer {payer_id} is already associated with rent account {account_id}."
            ) from error
        raise


def _parse_date(value: str | None, option_name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise RentalValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        ) from error
