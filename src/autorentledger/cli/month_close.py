"""CLI rendering for the derived monthly owner review."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from autorentledger.cli.common import DEFAULT_DATABASE, _format_currency
from autorentledger.month_close import MonthCloseStatus, MonthCloseSummary, build_month_close
from autorentledger.obligations import ObligationValidationError
from autorentledger.reconciliation import ReconciliationInvariantError
from autorentledger.review import ReviewInvariantError
from autorentledger.schedules import ObligationGenerationInvariantError
from autorentledger.storage import (
    SQLitePropertyCashRepository,
    SQLiteReconciliationRepository,
    SQLiteRentScheduleRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.suggestions import SuggestionInvariantError


def register_commands(subparsers) -> None:
    command = subparsers.add_parser(
        "month-close", help="show the derived read-only monthly owner review"
    )
    command.set_defaults(handler=_handle_month_close)
    command.add_argument("--period", required=True, help="canonical month YYYY-MM")
    command.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def _handle_month_close(args) -> int:
    return run_month_close(args.database, args.period)


def run_month_close(database_path: Path, period: str) -> int:
    try:
        summary = build_month_close(
            SQLiteReconciliationRepository(database_path),
            SQLiteReviewRepository(database_path),
            SQLiteSuggestionRepository(database_path),
            SQLiteRentScheduleRepository(database_path),
            SQLitePropertyCashRepository(database_path),
            period,
        )
    except (
        ObligationGenerationInvariantError,
        ObligationValidationError,
        ReconciliationInvariantError,
        ReviewInvariantError,
        SuggestionInvariantError,
        sqlite3.Error,
    ) as error:
        print(error)
        return 1
    print(render_month_close_terminal(summary))
    return 0


def render_month_close_terminal(summary: MonthCloseSummary) -> str:
    lines = [
        f"MONTH CLOSE - {summary.period}",
        f"STATUS: {summary.status.value.replace('_', ' ')}",
        "RENT",
        f"Obligations: {summary.rent.obligation_count}",
        f"Paid: {summary.rent.paid_count}",
        f"Partial: {summary.rent.partial_count}",
        f"Unpaid: {summary.rent.unpaid_count}",
        f"Owed: {_format_currency(summary.rent.owed_cents)}",
        f"Allocated: {_format_currency(summary.rent.allocated_cents)}",
        f"Remaining: {_format_currency(summary.rent.remaining_cents)}",
        "GLOBAL ATTENTION",
        f"Active observed payments: {summary.attention.active_payment_count}",
        f"Unresolved senders: {summary.attention.unresolved_sender_count}",
        f"Payments from unresolved senders: {summary.attention.unresolved_payment_count}",
        (f"Payments with unallocated money: {summary.attention.payments_with_unallocated_count}"),
        f"Unallocated money: {_format_currency(summary.attention.unallocated_cents)}",
        f"Unparsed evidence: {summary.attention.unparsed_evidence_count}",
        "SELECTED-MONTH SUGGESTIONS",
        (f"Actionable allocation suggestions: {summary.attention.actionable_suggestion_count}"),
        "RECURRING RENT",
        (
            "Missing expected obligations: "
            f"{summary.recurring_rent.missing_expected_obligation_count}"
        ),
        "EXPENSES",
        f"Operating: {_format_currency(summary.expenses.operating_expense_cents)}",
        (f"Capital improvements: {_format_currency(summary.expenses.capital_improvement_cents)}"),
        f"Active expenses: {summary.expenses.active_expense_count}",
        "PROPERTY CASH",
    ]
    if summary.property_cash:
        lines.extend(
            f"{item.property_name} (Property {item.property_id}): "
            f"owed {_format_currency(item.rent_owed_cents)}, "
            f"collected {_format_currency(item.rent_collected_cents)}, "
            f"operating {_format_currency(item.operating_expense_cents)}, "
            f"capital {_format_currency(item.capital_improvement_cents)}, "
            f"net {_format_currency(item.net_cash_before_debt_cents)}"
            for item in summary.property_cash
        )
    else:
        lines.append("No Properties found.")
    _append_details(lines, summary)
    return "\n".join(lines)


def _append_details(lines: list[str], summary: MonthCloseSummary) -> None:
    lines.append("NEXT ACTIONS")
    if summary.status is MonthCloseStatus.CLEAR:
        lines.append("No review items remain for this derived view.")
        return
    if summary.rent.partial_count or summary.rent.unpaid_count:
        open_ids = [
            str(item.obligation_id) for item in summary.rent.obligations if item.remaining_cents > 0
        ]
        lines.append(f"- Review open obligations: {', '.join(open_ids)}")
    if summary.attention.unresolved_sender_count:
        lines.append("- Review unresolved senders in Attention or Payments.")
    if summary.attention.unallocated_payments:
        payment_ids = ", ".join(
            str(item.reference_id) for item in summary.attention.unallocated_payments
        )
        lines.append(f"- Review payments with unallocated money: {payment_ids}")
    if summary.attention.actionable_suggestions:
        payment_ids = ", ".join(
            str(item.payment_event_id) for item in summary.attention.actionable_suggestions
        )
        lines.append(f"- Preview allocation suggestions for payments: {payment_ids}")
    if summary.attention.unparsed_evidence_count:
        lines.append("- Review unparsed evidence in Attention.")
    for item in summary.recurring_rent.missing_expected_obligations:
        lines.append(
            f"- Review missing obligation for account {item.rent_account_id} "
            f"from schedule {item.schedule_id}; preview generation before applying."
        )


__all__ = ["register_commands", "render_month_close_terminal", "run_month_close"]
