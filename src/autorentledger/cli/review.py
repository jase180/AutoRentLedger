"""Command-line entry point for AutoRentLedger."""

from __future__ import annotations

from pathlib import Path

from autorentledger.cli.common import (
    DEFAULT_DATABASE,
    _format_currency,
)
from autorentledger.reconciliation import (
    ReconciliationInvariantError,
)
from autorentledger.review import (
    ReviewInvariantError,
    ReviewKind,
    collect_review_items,
)
from autorentledger.storage import (
    SQLiteReconciliationRepository,
    SQLiteReviewRepository,
)


def register_commands(subparsers) -> None:
    review = subparsers.add_parser("review", help="show ledger items needing attention")
    review.add_argument("--database", type=Path, default=DEFAULT_DATABASE)


def run_review(database_path: Path) -> int:
    try:
        items = collect_review_items(
            SQLiteReconciliationRepository(database_path),
            SQLiteReviewRepository(database_path),
        )
    except (ReconciliationInvariantError, ReviewInvariantError) as error:
        print(error)
        return 1

    print(f"{'TYPE':<24} {'REF':<12} DETAILS")
    for item in items:
        if item.kind is ReviewKind.UNRESOLVED_PAYER:
            reference = "-"
            noun = "payment" if item.count == 1 else "payments"
            details = f"{item.summary} ({item.count} {noun})"
        elif item.kind is ReviewKind.UNALLOCATED_PAYMENT:
            reference = f"payment {item.reference_id}"
            details = f"{_format_currency(item.amount_cents)} remaining unallocated"
        elif item.kind in {
            ReviewKind.UNPAID_OBLIGATION,
            ReviewKind.PARTIAL_OBLIGATION,
        }:
            reference = f"oblig. {item.reference_id}"
            details = (
                f"{item.unit_label} / {item.account_display_name} / {item.period} / "
                f"{_format_currency(item.amount_cents)} remaining"
            )
        else:
            reference = f"raw {item.reference_id}"
            details = item.summary
        print(f"{item.kind:<24} {reference:<12} {details}")
    return 0
