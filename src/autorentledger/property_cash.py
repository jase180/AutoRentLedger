"""Derived monthly Property cash reporting without accounting writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from autorentledger.expenses import ExpenseCategory, expense_category_label
from autorentledger.obligations import parse_monthly_period
from autorentledger.storage import (
    PropertyCashExpenseRecord,
    SQLitePropertyCashRepository,
)


class PropertyCashPropertyNotFoundError(ValueError):
    """An explicitly selected Property does not exist."""


@dataclass(frozen=True)
class PropertyCashRentBreakdown:
    obligation_id: int
    rent_account_id: int
    property_id: int
    property_name: str
    unit_id: int
    unit_label: str
    account_display_name: str
    owed_cents: int
    collected_cents: int

    @property
    def display_name(self) -> str:
        return f"{self.property_name} / {self.unit_label} / {self.account_display_name}"


@dataclass(frozen=True)
class PropertyCashExpenseCategory:
    category: str
    label: str
    amount_cents: int


@dataclass(frozen=True)
class PropertyCashSummary:
    property_id: int
    property_name: str
    period: str
    rent_owed_cents: int
    rent_collected_cents: int
    operating_expense_cents: int
    capital_improvement_cents: int
    net_cash_before_debt_cents: int
    rent: tuple[PropertyCashRentBreakdown, ...]
    operating_categories: tuple[PropertyCashExpenseCategory, ...]
    expenses: tuple[PropertyCashExpenseRecord, ...]


@dataclass(frozen=True)
class PropertyCashPortfolio:
    period: str
    properties: tuple[PropertyCashSummary, ...]


def build_property_cash_summary(
    repository: SQLitePropertyCashRepository,
    period: str,
    *,
    property_id: int | None = None,
) -> PropertyCashPortfolio:
    """Derive Property cash strictly from obligations, allocations, and active expenses."""
    parsed_period = parse_monthly_period(period)
    properties = repository.list_properties()
    if property_id is not None:
        properties = [item for item in properties if item.property_id == property_id]
        if not properties:
            raise PropertyCashPropertyNotFoundError(
                f"Property {property_id} does not exist."
            )

    rent_records = repository.list_rent(parsed_period.value, property_id=property_id)
    expense_records = repository.list_active_expenses(
        parsed_period.first_day.isoformat(),
        (parsed_period.last_day + timedelta(days=1)).isoformat(),
        property_id=property_id,
    )
    rent_by_property: dict[int, list] = {}
    for record in rent_records:
        rent_by_property.setdefault(record.property_id, []).append(record)
    expenses_by_property: dict[int, list[PropertyCashExpenseRecord]] = {}
    for record in expense_records:
        expenses_by_property.setdefault(record.property_id, []).append(record)

    summaries = tuple(
        _build_one(
            item.property_id,
            item.property_name,
            parsed_period.value,
            rent_by_property.get(item.property_id, []),
            expenses_by_property.get(item.property_id, []),
        )
        for item in properties
    )
    return PropertyCashPortfolio(parsed_period.value, summaries)


def _build_one(property_id, property_name, period, rent_records, expenses):
    rent = tuple(
        PropertyCashRentBreakdown(
            obligation_id=item.obligation_id,
            rent_account_id=item.rent_account_id,
            property_id=item.property_id,
            property_name=item.property_name,
            unit_id=item.unit_id,
            unit_label=item.unit_label,
            account_display_name=item.account_display_name,
            owed_cents=item.owed_cents,
            collected_cents=item.collected_cents,
        )
        for item in rent_records
    )
    category_totals: dict[str, int] = {}
    for expense in expenses:
        category_totals[expense.category] = (
            category_totals.get(expense.category, 0) + expense.amount_cents
        )
    capital_value = ExpenseCategory.CAPITAL_IMPROVEMENT.value
    operating_categories = tuple(
        PropertyCashExpenseCategory(
            category.value,
            expense_category_label(category),
            category_totals[category.value],
        )
        for category in ExpenseCategory
        if category.value != capital_value and category_totals.get(category.value, 0)
    )
    rent_owed = sum(item.owed_cents for item in rent)
    rent_collected = sum(item.collected_cents for item in rent)
    operating = sum(
        item.amount_cents for item in expenses if item.category != capital_value
    )
    capital = category_totals.get(capital_value, 0)
    return PropertyCashSummary(
        property_id=property_id,
        property_name=property_name,
        period=period,
        rent_owed_cents=rent_owed,
        rent_collected_cents=rent_collected,
        operating_expense_cents=operating,
        capital_improvement_cents=capital,
        net_cash_before_debt_cents=rent_collected - operating - capital,
        rent=rent,
        operating_categories=operating_categories,
        expenses=tuple(expenses),
    )


__all__ = [
    "PropertyCashExpenseCategory",
    "PropertyCashPortfolio",
    "PropertyCashPropertyNotFoundError",
    "PropertyCashRentBreakdown",
    "PropertyCashSummary",
    "build_property_cash_summary",
]
