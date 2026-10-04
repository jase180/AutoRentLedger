"""CLI commands for explicit Property expenses."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autorentledger.cli.common import DEFAULT_DATABASE, _format_currency
from autorentledger.expenses import (
    ExpenseCategory,
    PropertyExpenseConflictError,
    PropertyExpenseMissingError,
    PropertyExpenseValidationError,
    create_property_expense,
    expense_category_label,
    get_property_expense,
    list_property_expenses,
    void_property_expense,
)
from autorentledger.storage import SQLitePropertyExpenseRepository


def register_commands(subparsers) -> None:
    expense = subparsers.add_parser("expense", help="manage explicit Property expenses")
    commands = expense.add_subparsers(dest="expense_command", required=True)

    add = commands.add_parser("add", help="record an owner-entered Property expense")
    add.set_defaults(handler=_handle_add)
    add.add_argument("--property", type=int, required=True)
    add.add_argument("--unit", type=int)
    add.add_argument("--date", required=True, dest="occurred_on")
    add.add_argument("--amount", required=True)
    add.add_argument(
        "--category",
        required=True,
        choices=[category.value for category in ExpenseCategory],
        help="controlled expense category",
    )
    add.add_argument("--vendor")
    add.add_argument("--note")
    add.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    show = commands.add_parser("show", help="inspect one expense and its void audit")
    show.set_defaults(handler=_handle_show)
    show.add_argument("expense_id", type=int)
    show.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    void = commands.add_parser("void", help="void an expense without deleting it")
    void.set_defaults(handler=_handle_void)
    void.add_argument("expense_id", type=int)
    void.add_argument("--reason", required=True)
    void.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    categories = commands.add_parser("categories", help="list controlled categories")
    categories.set_defaults(handler=_handle_categories)
    categories.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    expenses = subparsers.add_parser("expenses", help="list Property expenses")
    expenses.set_defaults(handler=_handle_listing)
    expenses.add_argument("--property", type=int)
    expenses.add_argument("--unit", type=int)
    expenses.add_argument("--from", dest="occurred_from")
    expenses.add_argument("--to", dest="occurred_to")
    expenses.add_argument(
        "--category", choices=[category.value for category in ExpenseCategory]
    )
    expenses.add_argument("--include-voided", action="store_true")
    expenses.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def _handle_add(args) -> int:
    return run_expense_add(
        args.database,
        property_id=args.property,
        unit_id=args.unit,
        occurred_on=args.occurred_on,
        amount=args.amount,
        category=args.category,
        vendor=args.vendor,
        note=args.note,
    )


def _handle_show(args) -> int:
    return run_expense_show(args.database, args.expense_id)


def _handle_void(args) -> int:
    return run_expense_void(args.database, args.expense_id, args.reason)


def _handle_categories(args) -> int:
    return run_expense_categories()


def _handle_listing(args) -> int:
    return run_expense_listing(
        args.database,
        property_id=args.property,
        unit_id=args.unit,
        occurred_from=args.occurred_from,
        occurred_to=args.occurred_to,
        category=args.category,
        include_voided=args.include_voided,
    )


def run_expense_add(
    database_path: Path,
    *,
    property_id: int,
    unit_id: int | None,
    occurred_on: str,
    amount: str,
    category: str,
    vendor: str | None,
    note: str | None,
) -> int:
    try:
        expense = create_property_expense(
            SQLitePropertyExpenseRepository(database_path),
            property_id=property_id,
            unit_id=unit_id,
            occurred_on=occurred_on,
            amount=amount,
            category=category,
            vendor=vendor,
            note=note,
        )
    except (
        PropertyExpenseConflictError,
        PropertyExpenseMissingError,
        PropertyExpenseValidationError,
        sqlite3.Error,
    ) as error:
        print(error)
        return 1
    print(f"Created expense {expense.id}: {_format_currency(expense.amount_cents)}")
    return 0


def run_expense_show(database_path: Path, expense_id: int) -> int:
    try:
        expense = get_property_expense(
            SQLitePropertyExpenseRepository(database_path), expense_id
        )
    except (PropertyExpenseMissingError, sqlite3.Error) as error:
        print(error)
        return 1
    print(f"Expense ID: {expense.id}")
    print(f"Date: {expense.occurred_on}")
    print(f"Property: {expense.property_name}")
    print(f"Unit: {expense.unit_label or '-'}")
    print(f"Category: {expense_category_label(expense.category)}")
    print(f"Vendor: {expense.vendor or '-'}")
    print(f"Amount: {_format_currency(expense.amount_cents)}")
    print(f"Note: {expense.note or '-'}")
    print(f"Status: {expense.status}")
    print(f"Created at: {expense.created_at}")
    if expense.voided_at is not None:
        print(f"Void reason: {expense.void_reason}")
        print(f"Voided at: {expense.void_created_at}")
    return 0


def run_expense_void(database_path: Path, expense_id: int, reason: str) -> int:
    try:
        expense, audit = void_property_expense(
            SQLitePropertyExpenseRepository(database_path), expense_id, reason
        )
    except (
        PropertyExpenseConflictError,
        PropertyExpenseMissingError,
        PropertyExpenseValidationError,
        sqlite3.Error,
    ) as error:
        print(error)
        return 1
    print(f"Voided expense {expense.id}.")
    print(f"Reason: {audit.reason}")
    return 0


def run_expense_listing(
    database_path: Path,
    *,
    property_id: int | None,
    unit_id: int | None,
    occurred_from: str | None,
    occurred_to: str | None,
    category: str | None,
    include_voided: bool,
) -> int:
    try:
        expenses = list_property_expenses(
            SQLitePropertyExpenseRepository(database_path),
            property_id=property_id,
            unit_id=unit_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            category=category,
            include_voided=include_voided,
        )
    except (PropertyExpenseValidationError, sqlite3.Error) as error:
        print(error)
        return 1
    print(
        f"{'ID':<5} {'DATE':<10} {'PROPERTY / UNIT':<32} "
        f"{'CATEGORY':<24} {'VENDOR':<24} {'AMOUNT':>12} STATUS"
    )
    for expense in expenses:
        print(
            f"{expense.id:<5} {expense.occurred_on:<10} "
            f"{expense.property_unit_display:<32} "
            f"{expense_category_label(expense.category):<24} "
            f"{(expense.vendor or '-'):<24} "
            f"{_format_currency(expense.amount_cents):>12} {expense.status}"
        )
    return 0


def run_expense_categories() -> int:
    for category in ExpenseCategory:
        print(f"{category.value:<24} {category.label}")
    return 0
