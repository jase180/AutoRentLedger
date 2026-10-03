"""Read-only composition for monthly Property cash screens."""

from pathlib import Path

from autorentledger.property_cash import (
    PropertyCashPortfolio,
    build_property_cash_summary,
)
from autorentledger.storage import SQLitePropertyCashRepository


def build_web_property_cash(
    database_path: Path, period: str, property_id: int | None = None
) -> PropertyCashPortfolio:
    return build_property_cash_summary(
        SQLitePropertyCashRepository(database_path), period, property_id=property_id
    )


__all__ = ["build_web_property_cash"]
