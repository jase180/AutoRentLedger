"""Canonical derivation of obligation reconciliation state."""

from dataclasses import dataclass
from enum import StrEnum

from autorentledger.obligations import parse_monthly_period
from autorentledger.storage import (
    ReconciliationSourceRecord,
    SQLiteReconciliationRepository,
)


class ReconciliationStatus(StrEnum):
    UNPAID = "UNPAID"
    PARTIAL = "PARTIAL"
    PAID = "PAID"


class ReconciliationInvariantError(RuntimeError):
    """Stored ledger rows violate allocation invariants."""


@dataclass(frozen=True)
class ReconciliationRecord:
    obligation_id: int
    rent_account_id: int
    unit_id: int
    unit_label: str
    account_display_name: str
    period: str
    due_date: str
    owed_cents: int
    allocated_cents: int
    remaining_cents: int
    status: ReconciliationStatus


def reconcile_period(
    repository: SQLiteReconciliationRepository, period: str
) -> list[ReconciliationRecord]:
    """Derive reconciliation for existing obligations in one canonical month."""
    canonical_period = parse_monthly_period(period).value
    return [
        _derive(source) for source in repository.list_sources_for_period(canonical_period)
    ]


def reconcile_all(
    repository: SQLiteReconciliationRepository,
) -> list[ReconciliationRecord]:
    """Derive reconciliation for every existing obligation."""
    return [_derive(source) for source in repository.list_sources()]


def get_reconciliation(
    repository: SQLiteReconciliationRepository, obligation_id: int
) -> ReconciliationRecord | None:
    """Derive reconciliation for one existing obligation."""
    source = repository.get_source(obligation_id)
    return _derive(source) if source else None


def _derive(source: ReconciliationSourceRecord) -> ReconciliationRecord:
    if source.allocated_cents > source.owed_cents:
        raise ReconciliationInvariantError(
            f"Reconciliation invariant violated for obligation {source.obligation_id}: "
            "allocated amount exceeds owed amount."
        )
    if source.allocated_cents < 0:
        raise ReconciliationInvariantError(
            f"Reconciliation invariant violated for obligation {source.obligation_id}: "
            "allocated amount is negative."
        )

    remaining_cents = source.owed_cents - source.allocated_cents
    if source.allocated_cents == 0:
        status = ReconciliationStatus.UNPAID
    elif remaining_cents == 0:
        status = ReconciliationStatus.PAID
    else:
        status = ReconciliationStatus.PARTIAL

    return ReconciliationRecord(
        obligation_id=source.obligation_id,
        rent_account_id=source.rent_account_id,
        unit_id=source.unit_id,
        unit_label=source.unit_label,
        account_display_name=source.account_display_name,
        period=source.period,
        due_date=source.due_date,
        owed_cents=source.owed_cents,
        allocated_cents=source.allocated_cents,
        remaining_cents=remaining_cents,
        status=status,
    )
