"""CLI rendering for the derived monthly Property cash summary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autorentledger.cli.common import DEFAULT_DATABASE, _format_currency
from autorentledger.expenses import expense_category_label
from autorentledger.obligations import ObligationValidationError
from autorentledger.property_cash import (
    PropertyCashPropertyNotFoundError,
    PropertyCashSummary,
    build_property_cash_summary,
)
from autorentledger.storage import SQLitePropertyCashRepository


def register_commands(subparsers) -> None:
    command = subparsers.add_parser(
        "property-cash", help="show derived monthly cash by Property"
    )
    command.add_argument("--period", required=True, help="canonical month YYYY-MM")
    command.add_argument("--property", type=int, dest="property_id")
    command.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def run_property_cash(
    database_path: Path, period: str, property_id: int | None = None
) -> int:
    try:
        portfolio = build_property_cash_summary(
            SQLitePropertyCashRepository(database_path),
            period,
            property_id=property_id,
        )
    except (ObligationValidationError, PropertyCashPropertyNotFoundError, sqlite3.Error) as error:
        print(error)
        return 1
    if property_id is not None:
        _print_property(portfolio.properties[0])
    else:
        _print_portfolio(portfolio.properties)
    return 0


def _print_portfolio(properties: tuple[PropertyCashSummary, ...]) -> None:
    print(
        f"{'PROPERTY':<24} {'OWED':>12} {'COLLECTED':>12} "
        f"{'OPERATING':>12} {'CAP IMPROV':>12} {'NET CASH':>12}"
    )
    for summary in properties:
        print(
            f"{summary.property_name:<24} "
            f"{_format_currency(summary.rent_owed_cents):>12} "
            f"{_format_currency(summary.rent_collected_cents):>12} "
            f"{_format_currency(summary.operating_expense_cents):>12} "
            f"{_format_currency(summary.capital_improvement_cents):>12} "
            f"{_format_currency(summary.net_cash_before_debt_cents):>12}"
        )


def _print_property(summary: PropertyCashSummary) -> None:
    print(f"{summary.property_name} — {summary.period}")
    print("\nCASH SUMMARY")
    _print_metric("Rent owed", summary.rent_owed_cents)
    _print_metric("Rent collected", summary.rent_collected_cents)
    _print_metric("Operating expenses", summary.operating_expense_cents)
    _print_metric("Capital improvements", summary.capital_improvement_cents)
    _print_metric("Net cash before debt", summary.net_cash_before_debt_cents)
    print("\nRENT")
    if summary.rent:
        for item in summary.rent:
            print(
                f"{item.display_name}: owed {_format_currency(item.owed_cents)}, "
                f"collected {_format_currency(item.collected_cents)}"
            )
    else:
        print("No rent obligations for this month.")
    print("\nEXPENSES")
    if summary.operating_categories:
        for item in summary.operating_categories:
            _print_metric(item.label, item.amount_cents)
    if summary.capital_improvement_cents:
        _print_metric("Capital Improvement", summary.capital_improvement_cents)
    if not summary.expenses:
        print("No active expenses for this month.")
    else:
        for expense in summary.expenses:
            print(
                f"{expense.occurred_on}  {expense.property_unit_display}  "
                f"{expense_category_label(expense.category)}  "
                f"{expense.vendor or '-'}  {_format_currency(expense.amount_cents)}"
            )


def _print_metric(label: str, amount_cents: int) -> None:
    print(f"{label:<26}{_format_currency(amount_cents):>14}")


__all__ = ["register_commands", "run_property_cash"]
