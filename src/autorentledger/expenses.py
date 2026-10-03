"""Explicit owner-recorded Property expense operations."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from autorentledger.obligations import ObligationValidationError, parse_currency_cents
from autorentledger.storage.expenses import (
    ExpensePropertyNotFoundError,
    ExpenseUnitNotFoundError,
    ExpenseUnitPropertyMismatchError,
    PropertyExpenseAlreadyVoidedError,
    PropertyExpenseNotFoundError,
    PropertyExpenseRecord,
    PropertyExpenseSummary,
    PropertyExpenseVoidRecord,
    SQLitePropertyExpenseRepository,
)


class ExpenseCategory(StrEnum):
    REPAIRS_MAINTENANCE = "repairs_maintenance"
    UTILITIES = "utilities"
    INSURANCE = "insurance"
    PROPERTY_TAX = "property_tax"
    MANAGEMENT = "management"
    CLEANING = "cleaning"
    LANDSCAPING_SNOW = "landscaping_snow"
    PEST_CONTROL = "pest_control"
    LEGAL_PROFESSIONAL = "legal_professional"
    CAPITAL_IMPROVEMENT = "capital_improvement"
    SUPPLIES = "supplies"
    OTHER = "other"

    @property
    def label(self) -> str:
        return EXPENSE_CATEGORY_LABELS[self]


EXPENSE_CATEGORY_LABELS = {
    ExpenseCategory.REPAIRS_MAINTENANCE: "Repairs & Maintenance",
    ExpenseCategory.UTILITIES: "Utilities",
    ExpenseCategory.INSURANCE: "Insurance",
    ExpenseCategory.PROPERTY_TAX: "Property Tax",
    ExpenseCategory.MANAGEMENT: "Management",
    ExpenseCategory.CLEANING: "Cleaning",
    ExpenseCategory.LANDSCAPING_SNOW: "Landscaping / Snow",
    ExpenseCategory.PEST_CONTROL: "Pest Control",
    ExpenseCategory.LEGAL_PROFESSIONAL: "Legal / Professional",
    ExpenseCategory.CAPITAL_IMPROVEMENT: "Capital Improvement",
    ExpenseCategory.SUPPLIES: "Supplies",
    ExpenseCategory.OTHER: "Other",
}


class PropertyExpenseValidationError(ValueError):
    pass


class PropertyExpenseMissingError(ValueError):
    pass


class PropertyExpenseConflictError(ValueError):
    pass


def create_property_expense(
    repository: SQLitePropertyExpenseRepository,
    *,
    property_id: int,
    occurred_on: str,
    amount: str,
    category: str | ExpenseCategory,
    unit_id: int | None = None,
    vendor: str | None = None,
    note: str | None = None,
) -> PropertyExpenseRecord:
    parsed_date = _canonical_date(occurred_on, "date")
    try:
        amount_cents = parse_currency_cents(amount)
    except ObligationValidationError as error:
        raise PropertyExpenseValidationError(str(error)) from error
    parsed_category = _category(category)
    try:
        return repository.create_checked(
            property_id=property_id,
            unit_id=unit_id,
            occurred_on=parsed_date,
            amount_cents=amount_cents,
            category=parsed_category.value,
            vendor=_optional_text(vendor),
            note=_optional_text(note),
        )
    except ExpensePropertyNotFoundError as error:
        raise PropertyExpenseMissingError(
            f"Property {property_id} does not exist."
        ) from error
    except ExpenseUnitNotFoundError as error:
        raise PropertyExpenseMissingError(f"Unit {unit_id} does not exist.") from error
    except ExpenseUnitPropertyMismatchError as error:
        raise PropertyExpenseConflictError(
            f"Unit {unit_id} does not belong to Property {property_id}."
        ) from error


def get_property_expense(
    repository: SQLitePropertyExpenseRepository, expense_id: int
) -> PropertyExpenseSummary:
    expense = repository.get_summary(expense_id)
    if expense is None:
        raise PropertyExpenseMissingError(f"Expense {expense_id} does not exist.")
    return expense


def list_property_expenses(
    repository: SQLitePropertyExpenseRepository,
    *,
    property_id: int | None = None,
    unit_id: int | None = None,
    occurred_from: str | None = None,
    occurred_to: str | None = None,
    category: str | ExpenseCategory | None = None,
    include_voided: bool = False,
) -> tuple[PropertyExpenseSummary, ...]:
    parsed_from = (
        _canonical_date(occurred_from, "from") if occurred_from is not None else None
    )
    parsed_to = _canonical_date(occurred_to, "to") if occurred_to is not None else None
    if parsed_from is not None and parsed_to is not None and parsed_from > parsed_to:
        raise PropertyExpenseValidationError("--from must not be after --to.")
    parsed_category = _category(category).value if category is not None else None
    return repository.list_summaries(
        property_id=property_id,
        unit_id=unit_id,
        occurred_from=parsed_from,
        occurred_to=parsed_to,
        category=parsed_category,
        include_voided=include_voided,
    )


def void_property_expense(
    repository: SQLitePropertyExpenseRepository, expense_id: int, reason: str
) -> tuple[PropertyExpenseRecord, PropertyExpenseVoidRecord]:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise PropertyExpenseValidationError("Void reason must not be blank.")
    try:
        return repository.void_checked(expense_id, normalized_reason)
    except PropertyExpenseNotFoundError as error:
        raise PropertyExpenseMissingError(f"Expense {expense_id} does not exist.") from error
    except PropertyExpenseAlreadyVoidedError as error:
        raise PropertyExpenseConflictError(
            f"Expense {expense_id} is already voided."
        ) from error


def expense_category_label(value: str | ExpenseCategory) -> str:
    return _category(value).label


def _category(value: str | ExpenseCategory) -> ExpenseCategory:
    try:
        return value if isinstance(value, ExpenseCategory) else ExpenseCategory(value)
    except ValueError as error:
        raise PropertyExpenseValidationError(f"Invalid expense category: {value}.") from error


def _canonical_date(value: str, option_name: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise PropertyExpenseValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        ) from error
    if parsed.isoformat() != value:
        raise PropertyExpenseValidationError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        )
    return value


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "EXPENSE_CATEGORY_LABELS",
    "ExpenseCategory",
    "PropertyExpenseConflictError",
    "PropertyExpenseMissingError",
    "PropertyExpenseValidationError",
    "create_property_expense",
    "expense_category_label",
    "get_property_expense",
    "list_property_expenses",
    "void_property_expense",
]
