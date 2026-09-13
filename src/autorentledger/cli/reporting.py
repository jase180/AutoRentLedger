"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

import csv
from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
)
from autorentledger.obligations import (
    ObligationValidationError,
)
from autorentledger.overview import build_owner_overview, render_owner_overview_terminal
from autorentledger.reconciliation import (
    ReconciliationInvariantError,
)
from autorentledger.reporting import MonthlyReport, ReportingInvariantError, build_monthly_report
from autorentledger.review import (
    ReviewInvariantError,
)
from autorentledger.schedules import (
    ObligationGenerationInvariantError,
)
from autorentledger.storage import (
    SQLiteReconciliationRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)
from autorentledger.suggestions import (
    SuggestionInvariantError,
)


def register_commands(subparsers) -> None:
    reconcile = subparsers.add_parser(
        "reconcile", help="derive obligation payment state for a period"
    )
    reconcile.add_argument("--period", required=True)
    reconcile.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    report = subparsers.add_parser("report", help="show a read-only monthly rent report")
    report.add_argument("--period", required=True)
    report.add_argument("--csv", type=Path, dest="csv_path")
    report.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    overview = subparsers.add_parser(
        "overview", help="show a consolidated read-only monthly owner snapshot"
    )
    overview.add_argument("--period", required=True)
    overview.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def run_report(database_path: Path, period: str, csv_path: Path | None = None) -> int:
    try:
        report = build_monthly_report(
            SQLiteReconciliationRepository(database_path),
            SQLiteReportingRepository(database_path),
            period,
        )
    except (
        ObligationValidationError,
        ReconciliationInvariantError,
        ReportingInvariantError,
    ) as error:
        print(error)
        return 1

    _print_monthly_report(report)
    if csv_path is not None:
        try:
            _write_report_csv(report, csv_path)
        except FileExistsError:
            print(f"CSV already exists; refusing to overwrite: {csv_path}")
            return 1
        except OSError as error:
            print(f"Could not write CSV {csv_path}: {error}")
            return 1
        print(f"CSV written: {csv_path}")
    return 0

def _print_monthly_report(report: MonthlyReport) -> None:
    print(f"Monthly Rent Report - {report.period}")
    print(
        f"{'UNIT':<12} {'ACCOUNT':<24} {'OWED':>12} "
        f"{'ALLOCATED':>12} {'REMAINING':>12} STATUS"
    )
    for row in report.obligations:
        print(
            f"{row.unit_label:<12} {row.account_display_name:<24} "
            f"{_format_currency(row.owed_cents):>12} "
            f"{_format_currency(row.allocated_cents):>12} "
            f"{_format_currency(row.remaining_cents):>12} {row.status}"
        )
    print("RENT TOTALS")
    print(f"Owed: {_format_currency(report.total_owed_cents)}")
    print(f"Allocated: {_format_currency(report.total_allocated_cents)}")
    print(f"Remaining: {_format_currency(report.total_remaining_cents)}")
    print("Obligations:")
    print(f"Paid: {report.paid_count}")
    print(f"Partial: {report.partial_count}")
    print(f"Unpaid: {report.unpaid_count}")
    print("PAYMENT INTAKE")
    print(f"Observed payments: {_format_currency(report.payment_received_cents)}")
    print(f"Allocated from payments: {_format_currency(report.payment_allocated_cents)}")
    print(f"Unallocated money: {_format_currency(report.payment_unallocated_cents)}")

def _write_report_csv(report: MonthlyReport, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "period",
                "obligation_id",
                "unit",
                "account",
                "due_date",
                "owed_cents",
                "allocated_cents",
                "remaining_cents",
                "status",
            ]
        )
        for row in report.obligations:
            writer.writerow(
                [
                    row.period,
                    row.obligation_id,
                    row.unit_label,
                    row.account_display_name,
                    row.due_date,
                    row.owed_cents,
                    row.allocated_cents,
                    row.remaining_cents,
                    row.status,
                ]
            )

def run_overview(database_path: Path, period: str) -> int:
    try:
        overview = build_owner_overview(
            SQLiteReconciliationRepository(database_path),
            SQLiteReportingRepository(database_path),
            SQLiteReviewRepository(database_path),
            SQLiteSuggestionRepository(database_path),
            SQLiteRentScheduleRepository(database_path),
            period,
        )
    except (
        ObligationGenerationInvariantError,
        ObligationValidationError,
        ReconciliationInvariantError,
        ReportingInvariantError,
        ReviewInvariantError,
        SuggestionInvariantError,
    ) as error:
        print(error)
        return 1
    print(render_owner_overview_terminal(overview))
    return 0
