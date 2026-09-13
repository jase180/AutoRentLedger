"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from pathlib import Path

from autorentledger.overview import OwnerOverview, build_owner_overview
from autorentledger.storage import (
    SQLiteReconciliationRepository,
    SQLiteRentScheduleRepository,
    SQLiteReportingRepository,
    SQLiteReviewRepository,
    SQLiteSuggestionRepository,
)


def build_web_owner_overview(database_path: Path, period: str) -> OwnerOverview:
    """Wire existing repositories into the canonical owner overview service."""
    return build_owner_overview(
        SQLiteReconciliationRepository(database_path),
        SQLiteReportingRepository(database_path),
        SQLiteReviewRepository(database_path),
        SQLiteSuggestionRepository(database_path),
        SQLiteRentScheduleRepository(database_path),
        period,
    )
