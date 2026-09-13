"""Read-only composition helpers shared by local web screens."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autorentledger.gmail_payments import GmailPaymentHistory, get_gmail_payment_history
from autorentledger.manual_payments import ManualPaymentHistory, get_manual_payment_history
from autorentledger.payment_listing import PaymentListRecord, list_payment_records
from autorentledger.storage import (
    LateFeeAllocationSummary,
    PaymentEventRecord,
    SQLiteAllocationRepository,
    SQLiteGmailPaymentRepository,
    SQLiteLateFeeAllocationRepository,
    SQLiteManualPaymentRepository,
    SQLiteObligationRepository,
    SQLitePaymentEventRepository,
    SQLitePaymentListingRepository,
)
from autorentledger.web.composition.common import WebDetailNotFoundError


@dataclass(frozen=True)
class PaymentsPage:
    """Visible payment records and exact totals for the active read-only filters."""

    records: tuple[PaymentListRecord, ...]
    unallocated_only: bool
    unresolved_only: bool
    observed_cents: int
    allocated_cents: int
    unallocated_cents: int

    @property
    def has_filters(self) -> bool:
        return self.unallocated_only or self.unresolved_only

@dataclass(frozen=True)
class PaymentAllocationDetail:
    allocation_id: int
    rent_obligation_id: int
    rent_account_id: int
    period: str
    unit_label: str
    account_display_name: str
    amount_cents: int

@dataclass(frozen=True)
class PaymentDetail:
    payment: PaymentListRecord
    event: PaymentEventRecord
    allocations: tuple[PaymentAllocationDetail, ...]
    late_fee_allocations: tuple[LateFeeAllocationSummary, ...]
    gmail_history: GmailPaymentHistory | None
    manual_history: ManualPaymentHistory | None

    @property
    def source_type(self) -> str:
        return "Manual" if self.event.manual_evidence_id is not None else "Gmail"

def build_web_payments(
    database_path: Path,
    *,
    unallocated_only: bool = False,
    unresolved_only: bool = False,
) -> PaymentsPage:
    """Filter canonical payment records and total only the visible result set."""
    records = list_payment_records(SQLitePaymentListingRepository(database_path))
    visible = tuple(
        record
        for record in records
        if (not unallocated_only or (record.voided_at is None and record.unallocated_cents > 0))
        and (not unresolved_only or (record.voided_at is None and record.payer_id is None))
    )
    active_visible = tuple(record for record in visible if record.voided_at is None)
    return PaymentsPage(
        records=visible,
        unallocated_only=unallocated_only,
        unresolved_only=unresolved_only,
        observed_cents=sum(record.amount_cents for record in active_visible),
        allocated_cents=sum(record.allocated_cents for record in active_visible),
        unallocated_cents=sum(record.unallocated_cents for record in active_visible),
    )

def build_web_payment_detail(
    database_path: Path, payment_event_id: int
) -> PaymentDetail:
    """Compose one normalized payment with canonical audit and allocation reads."""
    listing = list_payment_records(SQLitePaymentListingRepository(database_path))
    payment = next(
        (item for item in listing if item.payment_event_id == payment_event_id), None
    )
    event = SQLitePaymentEventRepository(database_path).get(payment_event_id)
    if payment is None or event is None:
        raise WebDetailNotFoundError(f"Payment {payment_event_id} does not exist.")

    obligation_repository = SQLiteObligationRepository(database_path)
    allocation_details: list[PaymentAllocationDetail] = []
    for allocation in SQLiteAllocationRepository(database_path).list_summaries(
        payment_event_id=payment_event_id
    ):
        obligation = obligation_repository.get_summary(allocation.rent_obligation_id)
        if obligation is None:
            raise RuntimeError("Payment allocation references a missing obligation.")
        allocation_details.append(
            PaymentAllocationDetail(
                allocation.id,
                allocation.rent_obligation_id,
                obligation.rent_account_id,
                allocation.period,
                allocation.unit_label,
                obligation.account_display_name,
                allocation.amount_cents,
            )
        )

    gmail_history = None
    manual_history = None
    if event.manual_evidence_id is not None:
        manual_history = get_manual_payment_history(
            SQLiteManualPaymentRepository(database_path), payment_event_id
        )
    else:
        gmail_history = get_gmail_payment_history(
            SQLiteGmailPaymentRepository(database_path), payment_event_id
        )
    return PaymentDetail(
        payment,
        event,
        tuple(allocation_details),
        SQLiteLateFeeAllocationRepository(database_path).list_summaries(
            payment_event_id=payment_event_id
        ),
        gmail_history,
        manual_history,
    )
