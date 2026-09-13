"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autorentledger.reconciliation import (
    ReconciliationRecord,
    ReconciliationStatus,
    reconcile_period,
)
from autorentledger.storage import (
    SQLiteReconciliationRepository,
)


@dataclass(frozen=True)
class ObligationsPage:
    """Selected-period canonical reconciliation records and their exact totals."""

    period: str
    records: tuple[ReconciliationRecord, ...]
    owed_cents: int
    allocated_cents: int
    remaining_cents: int
    paid_count: int
    partial_count: int
    unpaid_count: int

def build_web_obligations(database_path: Path, period: str) -> ObligationsPage:
    """Total actual obligations returned by canonical period reconciliation."""
    records = tuple(
        reconcile_period(SQLiteReconciliationRepository(database_path), period)
    )
    return ObligationsPage(
        period=period,
        records=records,
        owed_cents=sum(record.owed_cents for record in records),
        allocated_cents=sum(record.allocated_cents for record in records),
        remaining_cents=sum(record.remaining_cents for record in records),
        paid_count=sum(record.status is ReconciliationStatus.PAID for record in records),
        partial_count=sum(
            record.status is ReconciliationStatus.PARTIAL for record in records
        ),
        unpaid_count=sum(
            record.status is ReconciliationStatus.UNPAID for record in records
        ),
    )
