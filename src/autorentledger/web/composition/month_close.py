"""Read-only composition for the monthly owner review center."""

from pathlib import Path

from autorentledger.month_close import MonthCloseSummary, build_month_close
from autorentledger.storage import (
    SQLitePropertyCashRepository,
    SQLiteReconciliationRepository,
    SQLiteRentScheduleRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)


def build_web_month_close(database_path: Path, period: str) -> MonthCloseSummary:
    return build_month_close(
        SQLiteReconciliationRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        SQLitePropertyCashRepository(database_path),
        period,
    )


__all__ = ["build_web_month_close"]
