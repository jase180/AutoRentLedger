"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autorentledger.late_fees import list_late_fees
from autorentledger.reconciliation import (
    ReconciliationRecord,
    reconcile_all,
)
from autorentledger.storage import (
    LateFeeHistory,
    PayerRecord,
    RentAccountSummary,
    SQLiteAllocationRepository,
    SQLiteLateFeeRepository,
    SQLitePaymentEventRepository,
    SQLiteReconciliationRepository,
    SQLiteRentalRepository,
)
from autorentledger.web.composition.common import WebDetailNotFoundError


@dataclass(frozen=True)
class ContributingPaymentDetail:
    allocation_id: int
    payment_event_id: int
    occurred_on: str | None
    sender_name: str
    amount_cents: int

@dataclass(frozen=True)
class RentAccountObligationDetail:
    reconciliation: ReconciliationRecord
    contributions: tuple[ContributingPaymentDetail, ...]

@dataclass(frozen=True)
class RentAccountDetail:
    account: RentAccountSummary
    payers: tuple[PayerRecord, ...]
    obligations: tuple[RentAccountObligationDetail, ...]
    late_fees: tuple[LateFeeHistory, ...]

def build_web_rent_account_detail(
    database_path: Path, rent_account_id: int
) -> RentAccountDetail:
    """Compose account facts around canonical reconciliation records."""
    rental_repository = SQLiteRentalRepository(database_path)
    account = rental_repository.get_rent_account_summary(rent_account_id)
    if account is None:
        raise WebDetailNotFoundError(
            f"Rent account {rent_account_id} does not exist."
        )
    payers = tuple(rental_repository.list_account_payers(rent_account_id))
    reconciliations = sorted(
        (
            record
            for record in reconcile_all(SQLiteReconciliationRepository(database_path))
            if record.rent_account_id == rent_account_id
        ),
        key=lambda record: (record.period, record.due_date, record.obligation_id),
    )
    allocation_repository = SQLiteAllocationRepository(database_path)
    payment_repository = SQLitePaymentEventRepository(database_path)
    obligations: list[RentAccountObligationDetail] = []
    for reconciliation in reconciliations:
        contributions: list[ContributingPaymentDetail] = []
        for allocation in allocation_repository.list_summaries(
            rent_obligation_id=reconciliation.obligation_id
        ):
            payment = payment_repository.get(allocation.payment_event_id)
            if payment is None:
                raise RuntimeError("Obligation allocation references a missing payment.")
            contributions.append(
                ContributingPaymentDetail(
                    allocation.id,
                    payment.id,
                    payment.occurred_on,
                    payment.sender_name,
                    allocation.amount_cents,
                )
            )
        obligations.append(
            RentAccountObligationDetail(reconciliation, tuple(contributions))
        )
    late_fees = list_late_fees(
        SQLiteLateFeeRepository(database_path), account_id=rent_account_id
    )
    return RentAccountDetail(account, payers, tuple(obligations), late_fees)
