"""Read-only composition for Property expense web screens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autorentledger.expenses import get_property_expense, list_property_expenses
from autorentledger.storage import PropertyExpenseSummary, SQLitePropertyExpenseRepository
from autorentledger.web.composition.common import WebDetailNotFoundError


@dataclass(frozen=True)
class ExpensesPage:
    records: tuple[PropertyExpenseSummary, ...]
    include_voided: bool


def build_web_expenses(
    database_path: Path, *, include_voided: bool = False
) -> ExpensesPage:
    return ExpensesPage(
        list_property_expenses(
            SQLitePropertyExpenseRepository(database_path),
            include_voided=include_voided,
        ),
        include_voided,
    )


def build_web_expense_detail(
    database_path: Path, expense_id: int
) -> PropertyExpenseSummary:
    try:
        return get_property_expense(
            SQLitePropertyExpenseRepository(database_path), expense_id
        )
    except ValueError as error:
        raise WebDetailNotFoundError(str(error)) from error
