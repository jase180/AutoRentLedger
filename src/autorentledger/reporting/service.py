"""Read-only monthly owner reporting derived from canonical ledger services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from autorentledger.obligations import parse_monthly_period
from autorentledger.reconciliation import (
    ReconciliationRecord,
    ReconciliationStatus,
    reconcile_period,
)
from autorentledger.storage import (
    SQLiteReconciliationRepository,
    SQLiteReportingRepository,
)


class ReportingInvariantError(RuntimeError):
    """Stored ledger rows violate monthly reporting invariants."""


@dataclass(frozen=True)
class MonthlyReport:
    period: str
    obligations: tuple[ReconciliationRecord, ...]
    total_owed_cents: int
    total_allocated_cents: int
    total_remaining_cents: int
    paid_count: int
    partial_count: int
    unpaid_count: int
    payment_received_cents: int
    payment_allocated_cents: int
    payment_unallocated_cents: int


def build_monthly_report(
    reconciliation_repository: SQLiteReconciliationRepository,
    reporting_repository: SQLiteReportingRepository,
    period: str,
) -> MonthlyReport:
    """Compose obligation reconciliation and occurrence-dated payment intake."""
    parsed_period = parse_monthly_period(period)
    canonical_period = parsed_period.value
    obligations = tuple(reconcile_period(reconciliation_repository, canonical_period))

    total_owed_cents = sum(row.owed_cents for row in obligations)
    total_allocated_cents = sum(row.allocated_cents for row in obligations)
    total_remaining_cents = sum(row.remaining_cents for row in obligations)
    _require_nonnegative(
        "rent totals",
        total_owed_cents,
        total_allocated_cents,
        total_remaining_cents,
    )
    if total_owed_cents != total_allocated_cents + total_remaining_cents:
        raise ReportingInvariantError(
            "Monthly reporting invariant violated: rent owed does not equal "
            "allocated plus remaining."
        )

    start_on = parsed_period.first_day
    end_before = parsed_period.last_day + timedelta(days=1)
    payments = reporting_repository.list_payment_intake_sources(
        start_on.isoformat(), end_before.isoformat()
    )
    for payment in payments:
        _require_nonnegative(
            f"payment {payment.payment_event_id}",
            payment.amount_cents,
            payment.allocated_cents,
        )
        if payment.allocated_cents > payment.amount_cents:
            raise ReportingInvariantError(
                "Monthly reporting invariant violated for payment "
                f"{payment.payment_event_id}: allocated amount exceeds payment amount."
            )

    payment_received_cents = sum(payment.amount_cents for payment in payments)
    payment_allocated_cents = sum(payment.allocated_cents for payment in payments)
    payment_unallocated_cents = payment_received_cents - payment_allocated_cents
    _require_nonnegative(
        "payment totals",
        payment_received_cents,
        payment_allocated_cents,
        payment_unallocated_cents,
    )
    if payment_received_cents != payment_allocated_cents + payment_unallocated_cents:
        raise ReportingInvariantError(
            "Monthly reporting invariant violated: observed payments do not equal "
            "allocated plus unallocated."
        )

    return MonthlyReport(
        period=canonical_period,
        obligations=obligations,
        total_owed_cents=total_owed_cents,
        total_allocated_cents=total_allocated_cents,
        total_remaining_cents=total_remaining_cents,
        paid_count=sum(row.status is ReconciliationStatus.PAID for row in obligations),
        partial_count=sum(row.status is ReconciliationStatus.PARTIAL for row in obligations),
        unpaid_count=sum(row.status is ReconciliationStatus.UNPAID for row in obligations),
        payment_received_cents=payment_received_cents,
        payment_allocated_cents=payment_allocated_cents,
        payment_unallocated_cents=payment_unallocated_cents,
    )


def _require_nonnegative(context: str, *amounts: int) -> None:
    if any(amount < 0 for amount in amounts):
        raise ReportingInvariantError(
            f"Monthly reporting invariant violated for {context}: amount is negative."
        )
